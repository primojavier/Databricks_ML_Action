# Databricks notebook source
# MAGIC %md
# MAGIC Chapter 3: Building out our Bronze Layer
# MAGIC
# MAGIC ## Retrieval Augmented Generation Chatbot - Extracting chunks
# MAGIC
# MAGIC https://arxiv.org/pdf

# COMMAND ----------

# MAGIC %md
# MAGIC ### Run setup

# COMMAND ----------

# MAGIC %pip install transformers==4.30.2 "unstructured[pdf,docx]==0.10.30" langchain==0.0.319 llama-index==0.9.3 databricks-vectorsearch==0.20 pydantic==1.10.9 mlflow==2.9.0
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.dropdown(name='Reset', defaultValue='True', choices=['True', 'False'], label="Reset: Drop previous table")

# COMMAND ----------

# MAGIC %run ../../global-setup $project_name=rag_chatbot

# COMMAND ----------

table_name = "pdf_raw_text"
if bool(dbutils.widgets.get('Reset')):
  sql(f"DROP TABLE IF EXISTS {table_name}")
  sql(f"DROP TABLE IF EXISTS pdf_documentation_text")

# COMMAND ----------

from llama_index.langchain_helpers.text_splitter import SentenceSplitter
from llama_index import Document, set_global_tokenizer
from transformers import AutoTokenizer
from typing import Iterator
from pyspark.sql.functions import col, udf, length, pandas_udf, explode
import os
import pandas as pd 
from unstructured.partition.auto import partition
from mlia_utils.rag_funcs import *
import io

# Reduce the arrow batch size as our PDF can be big in memory
spark.conf.set("spark.sql.execution.arrow.maxRecordsPerBatch", 10)


# COMMAND ----------

# MAGIC %sql
# MAGIC --Note that we need to enable Change Data Feed on the table to create the index
# MAGIC CREATE TABLE IF NOT EXISTS pdf_documentation_text (
# MAGIC   id BIGINT GENERATED BY DEFAULT AS IDENTITY,
# MAGIC   pdf_name STRING,
# MAGIC   content STRING,
# MAGIC   embedding ARRAY <FLOAT>
# MAGIC   ) TBLPROPERTIES (delta.enableChangeDataFeed = true); 

# COMMAND ----------

documents_folder =  f"{volume_file_path}raw_documents/"
display(dbutils.fs.ls(f"{documents_folder}"))

# COMMAND ----------

# MAGIC %md 
# MAGIC ### Creating Table with Raw Data 
# MAGIC
# MAGIC This step is optional, you can keep your data in memory if you start with a small volume of examples and not required to keep original files. 

# COMMAND ----------

df = (
        spark.read.format("binaryfile")
        .option("recursiveFileLookup", "true")
        .load('dbfs:'+ documents_folder)
        )

df.write.mode("overwrite").saveAsTable(f"{catalog}.{database_name}.{table_name}")

# COMMAND ----------

display(sql(f"SELECT * FROM {table_name} LIMIT 2"))

# COMMAND ----------

# MAGIC %md 
# MAGIC ### Extract Text form PDFs into Chunks

# COMMAND ----------

# DBTITLE 1,Basic extracting
with open(f"{documents_folder}2303.10130.pdf", mode="rb") as pdf:
  doc = extract_doc_text(pdf.read())  
  print(doc)

# COMMAND ----------

@pandas_udf("array<string>")
def read_as_chunk(batch_iter: Iterator[pd.Series]) -> Iterator[pd.Series]:
    #set llama2 as tokenizer
    set_global_tokenizer(
      AutoTokenizer.from_pretrained("hf-internal-testing/llama-tokenizer")
    )
    #Sentence splitter from llama_index to split on sentences
    splitter = SentenceSplitter(chunk_size=500, chunk_overlap=50)
    def extract_and_split(b):
      txt = extract_doc_text(b)
      nodes = splitter.get_nodes_from_documents([Document(text=txt)])
      return [n.text for n in nodes]

    for x in batch_iter:
        yield x.apply(extract_and_split)


# COMMAND ----------

df_chunks = (df
                .withColumn("content", explode(read_as_chunk("content")))
                .selectExpr('path as pdf_name', 'content')
                )
display(df_chunks)

# COMMAND ----------

# MAGIC %md 
# MAGIC ## Converting text chunk into embeddings 

# COMMAND ----------

# MAGIC %md 
# MAGIC Here we are using Databricks Foundational API Model Serving. To learn more about check this documentation: 
# MAGIC - [AWS]()
# MAGIC - [Azure]() 

# COMMAND ----------

from mlflow.deployments import get_deploy_client

# bge-large-en Foundation models are available using the /serving-endpoints/databricks-bge-large-en/invocations api. 
deploy_client = get_deploy_client("databricks")

## NOTE: if you change your embedding model here, make sure you change it in the query step too
embeddings = deploy_client.predict(endpoint="databricks-bge-large-en", inputs={"input": ["What is ChatGPT?"]})
pprint(embeddings)

# COMMAND ----------

@pandas_udf("array<float>")
def get_embedding(contents: pd.Series) -> pd.Series:
    import mlflow.deployments
    deploy_client = mlflow.deployments.get_deploy_client("databricks")
    def get_embeddings(batch):
        #Note: this will gracefully fail if an exception is thrown during embedding creation (add try/except if needed) 
        response = deploy_client.predict(endpoint="databricks-bge-large-en", inputs={"input": batch})
        return [e['embedding'] for e in response.data]

    # Splitting the contents into batches of 150 items each, since the embedding model takes at most 150 inputs per request.
    max_batch_size = 150
    batches = [contents.iloc[i:i + max_batch_size] for i in range(0, len(contents), max_batch_size)]

    # Process each batch and collect the results
    all_embeddings = []
    for batch in batches:
        all_embeddings += get_embeddings(batch.tolist())

    return pd.Series(all_embeddings)

# COMMAND ----------

import pyspark.sql.functions as F

df_chunk_emd = (df_chunks
                .withColumn("embedding", get_embedding("content"))
                .selectExpr('pdf_name', 'content', 'embedding')
                )
display(df_chunk_emd)

# COMMAND ----------

df_chunk_emd.write.mode("append").saveAsTable(f"{catalog}.{database_name}.pdf_documentation_text")

# COMMAND ----------


