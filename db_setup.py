import mysql.connector
import json
import os

f = open("config.json")
config = json.load(f)

db = mysql.connector.connect(
    host=os.getenv("MYSQL_HOST"),
    user=os.getenv("MYSQL_USER"),
    password=os.getenv("MYSQL_PASSWORD"),
    database=os.getenv("MYSQL_DATABASE"),
)

cursor = db.cursor(buffered=True, dictionary=True)

# Create table named recipients if not exists
cursor.execute("SHOW TABLES")
cursor.execute("DROP TABLE IF EXISTS recipients")
print("Creating recipients table..")
cursor.execute(
    "CREATE TABLE recipients (name VARCHAR(255), recipient VARCHAR(255))"
)
print("Created recipients table.")

# Add names to table
for name in config["names"]:
    print(f"Inserting {name} into recipients.")
    sql = "INSERT INTO recipients (name, recipient) VALUES (%s, %s)"
    val = (name, "")
    cursor.execute(sql, val)

print("Saving changes.")
db.commit()
