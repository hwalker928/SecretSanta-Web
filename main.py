from flask import Flask, render_template, redirect
import mysql.connector
import random
import json
import os

f = open("config.json")
config = json.load(f)

app = Flask(__name__)


def connect_db():
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST"),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE"),
    )


@app.route("/")
def main():
    return render_template("index.html")


@app.route("/rules/<firstname>/<recipient>")
def rules(firstname, recipient):
    chosen_recipient_capital = recipient.capitalize()

    return render_template(
        "rules.html", chosen_recipient_capital=chosen_recipient_capital, config=config, firstname=firstname, reroll_enabled=config["reroll_enabled"]
    )


@app.route("/name/<firstname>")
def name(firstname: str):
    firstname_lower = firstname.lower()
    if firstname_lower not in config["names"]:
        return render_template(
            "error.html",
            message=f"Oh no! Make sure you have spelt your name correctly (I know, it is possible to spell your own name wrong sometimes.), else contact {config['admin_user']} if you believe there is a mistake.",
        )

    # Database connection
    database = connect_db()
    cursor = database.cursor()

    # Check if the user hasn't already been assigned a recipient
    cursor.execute(
        "SELECT * FROM recipients WHERE name = %s AND recipient = ''", (firstname_lower,)
    )
    result = cursor.fetchall()
    if not len(result) > 0:
        return render_template(
            "error.html",
            message=f"Oh no! It seems you have already been assigned a recipient. If you have forgotten who you were assigned, please contact {config['admin_user']}.",
        )
    
    # Choose random recipient
    cursor.execute("SELECT name FROM recipients WHERE name NOT IN (SELECT recipient FROM recipients) AND name != %s", (firstname_lower,))
    available_recipients = [row[0] for row in cursor.fetchall()]

    if not available_recipients:
        return render_template(
            "error.html",
            message=f"Oh no! It seems there are no available recipients left. Please contact {config['admin_user']} for assistance.",
        )

    chosen_recipient = random.choice(available_recipients)

    cursor.execute("DELETE FROM recipients WHERE name = %s", (firstname_lower,))

    sql = "INSERT INTO recipients (name, recipient) VALUES (%s, %s)"
    val = (firstname_lower, chosen_recipient)
    cursor.execute(sql, val)

    database.commit()
    database.close()

    return redirect(f"/rules/{firstname_lower}/{chosen_recipient}")


@app.route("/reroll/<firstname>", methods=["POST"])
def reroll(firstname):
    if not config["reroll_enabled"]:
        return render_template(
            "error.html",
            message=f"Oh no! Rerolling is disabled. Please contact {config['admin_user']} if you believe there is a mistake.",
        )

    firstname_lower = firstname.lower()
    if firstname_lower not in config["names"]:
        return render_template(
            "error.html",
            message=f"Oh no! Make sure you have spelt your name correctly (I know, it is possible to spell your own name wrong sometimes.), else contact {config['admin_user']} if you believe there is a mistake.",
        )
    
    # Database connection
    database = connect_db()
    cursor = database.cursor()
    
    cursor.execute(
        "UPDATE recipients SET recipient = '' WHERE name = %s", (firstname_lower,)
    )
     
    # Choose random recipient
    cursor.execute("SELECT name FROM recipients WHERE name NOT IN (SELECT recipient FROM recipients) AND name != %s", (firstname_lower,))
    available_recipients = [row[0] for row in cursor.fetchall()]

    if not available_recipients:
        return render_template(
            "error.html",
            message=f"Oh no! It seems there are no available recipients left. Please contact {config['admin_user']} for assistance.",
        )

    chosen_recipient = random.choice(available_recipients)

    sql = "UPDATE recipients SET recipient = %s WHERE name = %s"
    val = (chosen_recipient, firstname_lower)
    cursor.execute(sql, val)

    database.commit()
    database.close()

    # JSON return
    return json.dumps({"newName": chosen_recipient.capitalize()})


if __name__ == "__main__":
    db = connect_db()
    cursor = db.cursor()

    # Check if tables exist, if not, import db_setup
    cursor.execute("SHOW TABLES LIKE 'recipients'")
    result = cursor.fetchone()
    if not result:
        import db_setup

    app.run(host="0.0.0.0", debug=False, port=config["port"])
