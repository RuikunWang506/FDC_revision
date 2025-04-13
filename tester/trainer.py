from vanna.local import VannaLocal

# Initialize local model
vn = VannaLocal(model_path='sqlserver-model')

# Connect to SQL Server
conn_str = (
    "mssql+pyodbc://tristen:Fdc2022!!@192.168.254.204/oms_ef"
    "?driver=ODBC+Driver+17+for+SQL+Server"
)
vn.connect_to_mssql(connection_string=conn_str)

# Train from JSONL file
vn.train(jsonl_file_path="final_train_data_with_aggregates.jsonl")

# Save model locally
vn.save_to_file("sqlserver-model-trained.pkl")

print("✅ Training complete and model saved.")
