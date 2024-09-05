"""_summary_
RAG for your own documents
"""
import logging
import os
import sys
import uuid
import importlib
import base64
import inspect
import configparser
import subprocess
from pathlib import PurePath
from typing import List
from urllib.request import urlopen
from urllib.parse import quote
import chromadb
from flask import Flask, make_response, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename
from flask_cors import CORS, cross_origin
import openai
from langchain.schema.messages import HumanMessage, AIMessage
from langchain.chains.retrieval import create_retrieval_chain
from langchain.chains.history_aware_retriever import create_history_aware_retriever
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_chroma import Chroma
from langchain_community.document_loaders import DirectoryLoader,TextLoader, PyPDFDirectoryLoader
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import BaseMessage
from langchain_core.pydantic_v1 import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.llms.ollama import Ollama
from langchain_text_splitters import RecursiveCharacterTextSplitter, Language
from langchain_groq import ChatGroq, chat_models
from groq import Groq
import config

# The program needs a ID to be able to read the config
# Use _unittest for activating unittests or your own tests
if len(sys.argv) != 2:
    print("Error: argument missing -> ID")
    sys.exit(os.EX_USAGE)
rag_project=sys.argv[1]

# set working dir to program dir to allow relative paths in configs
wd = os.path.abspath(inspect.getsourcefile(lambda:0)).split("/")
wd.pop()
os.chdir('/'.join(map(str,wd)))

base_dir = os.path.abspath(os.path.dirname(__file__)) + '/'

# Read the constants from a config file
rc = configparser.ConfigParser()
rc.read(base_dir + "constants/constants_"+rag_project+".ini")

# max 16Mb for uploads
app = Flask(__name__)
maxmb = rc.getint('FLASK','max_mb_size')
app.config['MAX_CONTENT_LENGTH'] = maxmb * 1024 * 1024
CORS(app)

@app.context_processor
def context_processor():
    """ Store the globals in a Flask way """
    return dict()

def get_modelnames(mode, modeltext):
    """ 
        Load all API keys to environment vairiables.
        Return possible modelnames and check the chosen one.
    """
    names = []
    
    for llm in rc.get('LLMS','llms').split(','):
        option = llm+"_APIKEY"
        if option in config.apikeys:
            key = config.apikeys[option]
            env = llm+"_API_KEY"
            os.environ[env] = key

    if mode == 'OPENAI':
        client = openai.OpenAI(api_key = os.getenv('OPENAI_API_KEY')) 
    if mode == 'GROQ':
        client = Groq(api_key = os.environ.get("GROQ_API_KEY"))
    if mode == 'OLLAMA':
        client = None
        result = subprocess.run(['ollama', 'list'], stdout=subprocess.PIPE).stdout.decode('utf-8')
        namelist = result.strip().split()
        for name in namelist:
            hit = name.find(':latest')
            if hit>0:
                model = name[0:hit]
                names.append(model)            

    if client != None: # Must be made more explicit, Ollama doesn't have a client
        models = client.models.list().data
        names = [str(dict(modelitem)['id']) for modelitem in models]

    if not modeltext in names:
        logging.error("Model %s not found in %s models",modeltext, mode)
        sys.exit(os.EX_CONFIG)

    return names

# Configureer logging
logging.basicConfig(level=rc.get('DEFAULT','logging_level'))
logging.info("Working directory is %s", os.getcwd())

globvars = context_processor()
rcllms   = globvars['USE_LLM']     = rc.get('LLMS','use_llm')
rcmodel  = globvars['ModelText']   = rc.get('LLMS.'+rcllms,'modeltext')
rctemp   = globvars['Temperature'] = rc.getfloat('DEFAULT','temperature')
if rctemp < 0.0 or rctemp > 2.0:
    logging.error("Temperature not between 0.0 and 2.0: %f", rctemp)
    sys.exit(os.EX_CONFIG)
globvars['Chain']   = None
globvars['Store']   = {}
globvars['Session'] = uuid.uuid4()
modelnames = get_modelnames(rcllms, rcmodel)


if globvars['USE_LLM'] == "OPENAI":
    globvars['LLM']     = ChatOpenAI(model=rcmodel,temperature=rctemp)
if globvars['USE_LLM'] == "OLLAMA":
    globvars['LLM']     = Ollama(model=rcmodel)
if globvars['USE_LLM'] == "GROQ":
    globvars['LLM']     = ChatGroq(model=rcmodel)


if rcmodel not in modelnames:
    print(f"Error: modelname {rcmodel} not known with {rcllms}")
    sys.exit(os.EX_CONFIG)
class InMemoryHistory(BaseChatMessageHistory, BaseModel):
    """In memory implementation of chat message history."""

    messages: List[BaseMessage] = Field(default_factory=list)

    def add_messages(self, messages: List[BaseMessage]) -> None:
        """Add a list of messages to the store"""
        self.messages.extend(messages)

    def clear(self) -> None:
        self.messages = []

def get_session_history(session_id: str) -> BaseChatMessageHistory:
    """ Memory history store

    Args:
        session_id (str): no of the session

    Returns:
        BaseChatMessageHistory: the reference to the store
    """
    prefix = ""
    Store = globvars['Store']
    if session_id not in Store:
        Store[session_id] = InMemoryHistory()
    for message in Store[session_id].messages:
        if isinstance(message, AIMessage):
            prefix = "AI"
        else:
            prefix = "User"
        logging.info("%s: %s",prefix, message.content)
    return Store[session_id]


def encode_image(image_url) -> base64:
    """ Encode image to base64 """
    logging.info("Encoding image: %s",image_url)
    with urlopen(image_url) as url:
        f = url.read()
        image = base64.b64encode(f).decode("utf-8")
    return image

def embedding_function() -> OpenAIEmbeddings:
    """ Return an Embedding"""
    return OpenAIEmbeddings()

def initialize_chain(new_vectorstore=False):
    """ initialize the chain to access the LLM """

    this_model = globvars['LLM']
    text_loader_kwargs={'autodetect_encoding': True}
    collection_name="vectorstore"

    if not new_vectorstore and os.path.exists(rc.get('DEFAULT','persistence')+'/chroma.sqlite3'):
        persistent_client = chromadb.PersistentClient(
            path=rc.get('DEFAULT','persistence'))
        vectorstore = Chroma(client=persistent_client,
                             collection_name=collection_name,
                             embedding_function=OpenAIEmbeddings())
        logging.info("Loaded %s chunks from persistent vectorstore", len(vectorstore.get()['ids']))
    else:
        persistent_client = chromadb.PersistentClient(
            path=rc.get('DEFAULT','persistence'))

        # Since we can't detect existence of a collection, we create one before deleting
        # Only needed in a start condition where no persitent store was found.
        # This way all files are deleted compared to .reset which only disconnects.
        persistent_client.get_or_create_collection(name=collection_name)
        persistent_client.delete_collection(name=collection_name)
        vectorstore = Chroma(client=persistent_client,
                             collection_name=collection_name,
                             embedding_function=OpenAIEmbeddings())
        # Delete previous stored documents

        # Load text files
        loader = DirectoryLoader(path=rc.get('DEFAULT','data_dir'),
                                 glob=rc.get('DEFAULT','data_glob_txt'),
                                 loader_cls=TextLoader,
                                 loader_kwargs=text_loader_kwargs)
        docs = loader.load()
        logging.info("Context loaded from %s text documents...",str(len(docs)))

        if rc.has_option('DEFAULT','LANGUAGE'):
            text_splitter = RecursiveCharacterTextSplitter.from_language(
                language=Language[rc.get('DEFAULT','language')],
                chunk_size=rc.getint('DEFAULT','chunk_size'),
                chunk_overlap=rc.getint('DEFAULT','chunk_overlap'))
        else:
            text_splitter = RecursiveCharacterTextSplitter(
                chunk_size=rc.getint('DEFAULT','chunk_size'),
                chunk_overlap=rc.getint('DEFAULT','chunk_overlap'))

        splits = text_splitter.split_documents(docs)
        logging.info("...resulting in %s splits",len(splits))
        # Create the vectorstore

        if len(splits)>0:
            vectorstore.add_documents(documents=splits)

        # Load PDF's
        loader = PyPDFDirectoryLoader(path=rc.get('DEFAULT','data_dir'),
                                      glob=rc.get('DEFAULT','data_glob_pdf'))
        splits = loader.load_and_split()
        logging.info("Context loaded from PDF documents, %s splits",str(len(splits)))
        if len(splits)>0:
            vectorstore.add_documents(splits)
        logging.info("Stored %s chunks into vectorstore",len(vectorstore.get()['ids']))

    retriever = vectorstore.as_retriever(search_kwargs={"k": 8})

    ### Contextualize question ###
    contextualize_q_system_prompt = rc.get('DEFAULT','contextualize_q_system_prompt')
    contextualize_q_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", contextualize_q_system_prompt),
            MessagesPlaceholder("chat_history"),
            ("human", "{input}"),
        ]
    )
    history_aware_retriever = create_history_aware_retriever(
        this_model, retriever, contextualize_q_prompt
    )

    ### Answer question ###
    system_prompt = rc.get('DEFAULT','system_prompt') + (
        "{context}"
    )
    qa_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            MessagesPlaceholder("chat_history"),
            ("human", "{input}"),
        ]
    )
    question_answer_chain = create_stuff_documents_chain(this_model, qa_prompt)

    rag_chain = create_retrieval_chain(history_aware_retriever, question_answer_chain)

    globvars['Chain'] = RunnableWithMessageHistory(
        rag_chain,
        get_session_history=get_session_history,
        input_messages_key="input",
        history_messages_key="chat_history",
        output_messages_key="answer",
    )
    logging.info("Chain initialized: %s",globvars['ModelText'])
    return True

def create_call(name, function, methods, params=[], response_output=True):
    """ Create Flask route """
    app_path = "/prompt/"+rc.get('DEFAULT','id')
    #@app.route(app_path+name, methods=methods)
    @cross_origin()
    def _function():
        if len(request.files) > 0:
            values = request.files
        else:
            values = []
            for param in params:
                    if request.method == 'GET':
                        values.append(request.values[param])
                        logging.info("%s: %s", param, values[:-1])
                    if request.method == 'POST':
                        values.append(request.form[param])
                        logging.info("%s: %s", param, values[:-1])
        try: 
            result = function(values)
            if response_output:
                logging.info("Result %s: %s",name, result['answer'])
                return make_response(result['answer'], 200)
            else:
                logging.info("Processing %s succeded",name)
                return result
        except HTTPException as e:
            logging.error("Error processing %s: %s",name, str(e))
            return make_response(f"Error processing {name}", 500)
    _function.__name__ = 'p'+name
    if name == '':
        my_path = app_path
    else:
        my_path = '/'.join([app_path,name])
    logging.info("path -> "+my_path+" "+','.join(params))
    app.add_url_rule(my_path, name, _function, methods=methods)

def prompt(values):
    """ Answer the prompt """
    prompt = values[0]
    return globvars['Chain'].invoke(
            {"input": prompt},
            config={"configurable": {"session_id": globvars['Session']}},
        )

create_call('', prompt, ["GET", "POST"], ['prompt'])

def model(values):
    """ Set the LLM model """
    this_model = values[0]
    if not this_model.isascii():
        print(this_model)
    if not this_model in modelnames:
        error_text = "Model "+this_model+" not found in OpenAI's models"
        logging.error(error_text)
        raise HTTPException
    globvars['ModelText'] = this_model
    globvars['LLM'] = ChatOpenAI(model=globvars['ModelText'],
                            temperature=globvars['Temperature'])
    initialize_chain(True)
    return {'answer':'Model set to '+this_model}
    
create_call('model', model, ["GET", "POST"], ['model'])

def temp(values):
    """ Set temperature """
    temperature = float(values[0])
    if temperature < 0.0 or temperature > 2.0:
        error_text = "Temperature "+str(temperature)+" not between 0.0 and 2.0"
        logging.error(error_text)
        raise HTTPException(error_text)
    globvars['Temperature'] = temperature
    globvars['LLM'] = ChatOpenAI(model=globvars['ModelText'],
                        temperature=globvars['Temperature'])
    initialize_chain(True)
    return {'answer':'Temperature set to '+str(temperature)}

create_call('temp', temp, ["GET", "POST"], ['temp'])

def reload(values):
    """ Reload documents """
    initialize_chain(True)
    return {'answer':'Documents reloaded'}

create_call('reload', reload, ["GET", "POST"])

def clear(values):
    """ Clear the cache """
    globvars['Store'].clear()
    return {'answer':'History deleted'}

create_call('clear', clear, ["GET", "POST"])

def cache(values):
    """ Return cache contents """
    content = ""
    if globvars['Session'] in globvars['Store']:
        for message in globvars['Store'][globvars['Session']].messages:
            if isinstance(message, AIMessage):
                prefix = "AI"
            else:
                prefix = "User"
            content += prefix +  ":" + str(message) + "\n"
    return {'answer': content}

create_call('cache', cache, ["GET", "POST"])

def send_files(values):
    """ Serve HTML files """
    file = values[0]
    html_dir = rc.get('DEFAULT','html')
    absolute_path = html_dir[0:1] == '/'
    if absolute_path:
        serve_file = os.path.normpath(os.path.join(html_dir,file))
    else:
        serve_file = os.path.normpath(os.path.join(base_dir + html_dir,file))
    if not serve_file.startswith(base_dir):
        raise HTTPException("Parameter value for HTML not allowed")
    return send_file(serve_file)

create_call('files', send_files, ["GET"], ['file'], False)

def process_image(values):
    """ Send image to ChatGPT and send prompt to analyse contents """
    logging.info(globvars['ModelText'])
    my_url = values[0]
    text   = values[1]
    if not my_url.isalnum:
        raise HTTPException("URL is not well-formed")
    if globvars['ModelText'] != 'gpt-4o':
        raise HTTPException("Image processing only available in gpt-4o")

    image_url = quote(my_url, safe='/:?=&')
    logging.info("Processing image: %s, with prompt: %s", image_url, text)
    bimage = encode_image(image_url)
    chain = ChatOpenAI(model=globvars['ModelText'],
                        temperature=globvars['Temperature'])
    msg = chain.invoke(
        [
            AIMessage(content="Picture revealer"),
            HumanMessage(content=[
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{bimage}"}}
            ])
        ]
    )
    return {'answer': msg.content}

create_call('image', process_image, ["GET"], ['image','prompt'])

def upload_file(values):
    """ Handles the file upload """

    file = values['file']
    if file.filename == '':
        error_text = "Error in upload data, no filename found"
        logging.error(error_text)
        raise HTTPException(error_text)

    filename = secure_filename(file.filename)

    # module 'constants' is not iterable, so repeating code unfortunately
    found = False
    if PurePath(filename).match(rc.get('DEFAULT','data_glob_txt')):
        found = True
    if PurePath(filename).match(rc.get('DEFAULT','data_glob_pdf')):
        found = True

    if not found:
        error_text = "File to upload has extension that doesn't match GLOB_* constants"
        logging.error(error_text)
        raise HTTPException(error_text)

    filepath = os.path.join(rc.get('DEFAULT','data_dir'),filename)

    logging.info("Saving %s on: %s",filename,filepath)
    file.save(filepath)
    logging.info("File %s saved on: %s",filename,filepath)
    initialize_chain(True)
    return {'answer': 'Upload of '+filename+' completed'}

create_call('upload', upload_file, ["POST"])
    
@app.teardown_request
def log_unhandled(e):
    if e is not None:
        print(repr(e))
        
if __name__ == '__main__':
    if initialize_chain():
        app.run(port=rc.get('FLASK','port'), debug=rc.getboolean('FLASK','debug'), host="0.0.0.0")
    else:
        logging.error("Initialization of chain failed")
