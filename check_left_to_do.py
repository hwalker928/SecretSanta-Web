import mysql.connector
import os

db = mysql.connector.connect(
    host=os.getenv("MYSQL_HOST"),
    user=os.getenv("MYSQL_USER"),
    password=os.getenv("MYSQL_PASSWORD"),
    database=os.getenv("MYSQL_DATABASE"),
)

cursor = db.cursor()

cursor.execute("SELECT * FROM recipients WHERE recipient=''")

result = cursor.fetchall()

for x in result:
    print(x[0].capitalize())
