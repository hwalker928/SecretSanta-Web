from flask import Flask, render_template, redirect, jsonify
import mysql.connector
import random
import logging
import os
import datetime

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Load configuration
config = {
    "port": int(os.getenv("APP_PORT", 5000)),
    "reroll_enabled": os.getenv("REROLL_ENABLED", "true").lower() == "true",
    "admin_user": os.getenv("ADMIN_USER", "admin"),
    "names": os.getenv("VALID_NAMES", "").split(","),
    "year": os.getenv("YEAR", datetime.datetime.now().year),
    "budget": os.getenv("BUDGET", "10")
}

app = Flask(__name__)

# Database connection function
def connect_db():
    try:
        return mysql.connector.connect(
            host=os.getenv("MYSQL_HOST"),
            user=os.getenv("MYSQL_USER"),
            password=os.getenv("MYSQL_PASSWORD"),
            database=os.getenv("MYSQL_DATABASE"),
        )
    except mysql.connector.Error as e:
        logging.error(f"Database connection failed: {e}")
        raise

@app.route("/")
def main():
    return render_template("index.html")

@app.route("/rules/<firstname>/<recipient>")
def rules(firstname, recipient):
    return render_template(
        "rules.html",
        chosen_recipient_capital=recipient.capitalize(),
        config=config,
        firstname=firstname,
        reroll_enabled=config["reroll_enabled"]
    )

@app.route("/name/<firstname>")
def name(firstname: str):
    firstname_lower = firstname.lower()
    if firstname_lower not in config["names"]:
        return render_template(
            "error.html",
            message=(
                "Oh no! Make sure you have spelled your name correctly. "
                f"Contact {config['admin_user']} if you believe there is a mistake."
            ),
        )

    try:
        with connect_db() as database:
            with database.cursor(dictionary=True, buffered=True) as cursor:
                # Check if the user has already been assigned a recipient
                cursor.execute(
                    "SELECT * FROM recipients WHERE name = %s AND recipient != ''",
                    (firstname_lower,)
                )
                if cursor.fetchone():
                    return render_template(
                        "error.html",
                        message=f"You have already been assigned a recipient. Contact {config['admin_user']} for assistance."
                    )

                # Get available recipients
                cursor.execute(
                    "SELECT name FROM recipients WHERE name NOT IN "
                    "(SELECT recipient FROM recipients) AND name != %s",
                    (firstname_lower,)
                )
                available_recipients = [row["name"] for row in cursor.fetchall()]

                if not available_recipients:
                    return render_template(
                        "error.html",
                        message="No available recipients left. Contact admin for assistance."
                    )

                chosen_recipient = random.choice(available_recipients)

                # Update the database with the assigned recipient
                cursor.execute(
                    "INSERT INTO recipients (name, recipient) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE recipient = %s",
                    (firstname_lower, chosen_recipient, chosen_recipient)
                )
                database.commit()

        return redirect(f"/rules/{firstname_lower}/{chosen_recipient}")

    except mysql.connector.Error as e:
        logging.error(f"Database error: {e}")
        return render_template("error.html", message="Internal server error. Please try again later.")

@app.route("/reroll/<firstname>", methods=["POST"])
def reroll(firstname):
    if not config["reroll_enabled"]:
        return render_template(
            "error.html",
            message="Rerolling is disabled. Contact admin for assistance."
        )

    firstname_lower = firstname.lower()
    if firstname_lower not in config["names"]:
        return render_template(
            "error.html",
            message=(
                "Oh no! Make sure you have spelled your name correctly. "
                f"Contact {config['admin_user']} if you believe there is a mistake."
            ),
        )

    try:
        with connect_db() as database:
            with database.cursor(dictionary=True, buffered=True) as cursor:
                # Reset the recipient
                cursor.execute("UPDATE recipients SET recipient = '' WHERE name = %s", (firstname_lower,))

                # Get new available recipients
                cursor.execute(
                    "SELECT name FROM recipients WHERE name NOT IN "
                    "(SELECT recipient FROM recipients) AND name != %s",
                    (firstname_lower,)
                )
                available_recipients = [row["name"] for row in cursor.fetchall()]

                if not available_recipients:
                    return render_template(
                        "error.html",
                        message="No available recipients left. Contact admin for assistance."
                    )

                chosen_recipient = random.choice(available_recipients)

                # Update the database with the new recipient
                cursor.execute(
                    "UPDATE recipients SET recipient = %s WHERE name = %s",
                    (chosen_recipient, firstname_lower)
                )
                database.commit()

        return jsonify({"newName": chosen_recipient.capitalize()})

    except mysql.connector.Error as e:
        logging.error(f"Database error: {e}")
        return render_template("error.html", message="Internal server error. Please try again later.")

if __name__ == "__main__":
    try:
        with connect_db() as db:
            with db.cursor() as cursor:
                cursor.execute("SHOW TABLES LIKE 'recipients'")
                result = cursor.fetchone()
                if not result:
                    import db_setup
                    db_setup.setup(config["names"])
        app.run(host="0.0.0.0", debug=False, port=config["port"])
    except Exception as e:
        logging.critical(f"Failed to start the application: {e}")
