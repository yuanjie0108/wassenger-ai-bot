import os
import threading
import requests
import psycopg2
from flask import Flask, request, jsonify
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, timedelta
import time

load_dotenv()

app = Flask(__name__)

# --- Configuration & API Clients ---
WASSENGER_API_URL = "https://api.wassenger.com/v1"
WASSENGER_API_KEY = os.getenv("WASSENGER_API_KEY")
OPENAI_CLIENT = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

DATABASE_URL = os.getenv("DATABASE_URL")

# Connect to the database
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def setup_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS follow_ups (
            contact_id VARCHAR(255) PRIMARY KEY,
            phone_number VARCHAR(255),
            status VARCHAR(50),
            scheduled_time TIMESTAMP,
            history JSONB
        );
    """)
    conn.commit()
    cursor.close()
    conn.close()

# --- Helper Functions ---
def send_message_to_wassenger(phone, message_content):
    headers = {"Authorization": f"Bearer {WASSENGER_API_KEY}", "Content-Type": "application/json"}
    payload = {"phone": phone, "message": message_content}
    try:
        response = requests.post(f"{WASSENGER_API_URL}/messages", json=payload, headers=headers)
        response.raise_for_status()
        print(f"Message sent successfully to {phone}.")
    except requests.exceptions.HTTPError as err:
        print(f"HTTP Error: {err.response.text}")
    except Exception as e:
        print(f"An error occurred while sending message: {e}")

def send_initial_follow_up(contact_id, phone_number):
    try:
        initial_prompt = "You are a helpful medical assistant. A patient needs a follow-up. Please write a polite message to ask how they are doing after their recent appointment and if they have any questions. Keep it under 100 words."
        completion = OPENAI_CLIENT.chat.completions.create(
            model="gpt-4o", 
            messages=[{"role": "system", "content": initial_prompt}]
        )
        message_text = completion.choices[0].message.content

        send_message_to_wassenger(phone_number, message_text)
        
        # Update database
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT history FROM follow_ups WHERE contact_id = %s", (contact_id,))
        history = cursor.fetchone()[0] if cursor.rowcount > 0 else []
        history.append({"role": "assistant", "content": message_text})
        cursor.execute("UPDATE follow_ups SET status = %s, history = %s WHERE contact_id = %s", ('ongoing', psycopg2.extras.Json(history), contact_id))
        conn.commit()
        cursor.close()
        conn.close()
        
        print(f"Initial follow-up sent to {phone_number}.")
    except Exception as e:
        print(f"Error sending follow-up to {phone_number}: {e}")

def handle_ai_reply(contact_id, phone_number, message_content):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT history FROM follow_ups WHERE contact_id = %s", (contact_id,))
        result = cursor.fetchone()
        cursor.close()
        
        if not result or result[0]['status'] != 'ongoing':
            conn.close()
            print(f"No ongoing conversation for {phone_number}. Skipping AI reply.")
            return

        history = result[0]
        history.append({"role": "user", "content": message_content})
        
        system_prompt = "You are a professional medical assistant replying to a patient. Be helpful, concise, and empathetic. Do not give medical advice. If the patient asks for an appointment or to speak with a doctor, tell them you will connect them with a human."
        
        messages = [{"role": "system", "content": system_prompt}] + history
        
        completion = OPENAI_CLIENT.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        ai_reply = completion.choices[0].message.content
        
        send_message_to_wassenger(phone_number, ai_reply)
        
        history.append({"role": "assistant", "content": ai_reply})
        
        # Update history in database
        cursor = conn.cursor()
        cursor.execute("UPDATE follow_ups SET history = %s WHERE contact_id = %s", (psycopg2.extras.Json(history), contact_id))
        conn.commit()
        cursor.close()
        conn.close()
        
        print(f"AI replied to {phone_number} with: {ai_reply}")
    except Exception as e:
        print(f"Error generating AI reply for {phone_number}: {e}")

# --- Background Worker Thread ---
def background_worker():
    while True:
        conn = get_db_connection()
        cursor = conn.cursor()
        now = datetime.now()
        
        # Check for scheduled follow-ups that are due
        cursor.execute("SELECT contact_id, phone_number FROM follow_ups WHERE status = %s AND scheduled_time < %s", ('scheduled', now))
        due_follow_ups = cursor.fetchall()
        
        for contact_id, phone_number in due_follow_ups:
            send_initial_follow_up(contact_id, phone_number)
            
        cursor.close()
        conn.close()
        time.sleep(60) # Check every minute

@app.before_first_request
def start_worker():
    setup_db()
    threading.Thread(target=background_worker, daemon=True).start()

# --- Webhook Endpoint ---
@app.route("/wassenger-webhook/", methods=["POST"])
def wassenger_webhook():
    payload = request.json
    event_type = payload.get("event")
    
    contact_id = payload.get("id") or payload.get("data", {}).get("wid") or payload.get("data", {}).get("contact", {}).get("id")
    phone_number = payload.get("data", {}).get("phone") or payload.get("data", {}).get("contact", {}).get("phone")
    
    if not phone_number or not contact_id:
        print("Webhook received with missing phone number or contact ID. Skipping.")
        return jsonify({"status": "error", "message": "Missing key data"}), 400

    # Handle the contact update event
    if event_type == "contact:update":
        labels = payload.get("data", {}).get("chat", {}).get("labels", [])
        
        if "Follow-up" in labels:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT contact_id FROM follow_ups WHERE contact_id = %s", (contact_id,))
            if cursor.fetchone():
                conn.close()
                print(f"Follow-up for {phone_number} already exists.")
                return jsonify({"status": "success", "message": "Follow-up already exists"}), 200

            scheduled_time = datetime.now() + timedelta(minutes=1) # 1-minute delay for testing
            
            cursor.execute("INSERT INTO follow_ups (contact_id, phone_number, status, scheduled_time, history) VALUES (%s, %s, %s, %s, %s)",
                           (contact_id, phone_number, 'scheduled', scheduled_time, psycopg2.extras.Json([])))
            conn.commit()
            cursor.close()
            conn.close()
            
            print(f"Follow-up label detected for {phone_number}. Scheduling message in database.")
            return jsonify({"status": "success", "message": "Follow-up scheduled"}), 200

    # Handle an incoming message from a patient
    elif event_type == "message:in:new":
        message_data = payload.get("data", {})
        message_body = message_data.get("content", "").strip()
        
        # Check for the trigger keyword
        if message_data.get("fromMe") is True and message_body.upper() == "START FOLLOWUP":
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT contact_id FROM follow_ups WHERE contact_id = %s", (contact_id,))
            if cursor.fetchone():
                conn.close()
                print(f"Follow-up for {phone_number} already exists.")
                return jsonify({"status": "success", "message": "Follow-up already exists"}), 200

            scheduled_time = datetime.now() + timedelta(minutes=1) # 1-minute delay for testing
            
            cursor.execute("INSERT INTO follow_ups (contact_id, phone_number, status, scheduled_time, history) VALUES (%s, %s, %s, %s, %s)",
                           (contact_id, phone_number, 'scheduled', scheduled_time, psycopg2.extras.Json([])))
            conn.commit()
            cursor.close()
            conn.close()
            
            print(f"Message trigger detected for {phone_number}. Scheduling message in database.")
            return jsonify({"status": "success", "message": "Follow-up scheduled"}), 200

        # Handle a regular patient reply if an ongoing conversation exists
        elif message_data.get("fromMe") is False:
            threading.Thread(target=handle_ai_reply, args=[contact_id, phone_number, message_body]).start()

        else:
            print(f"Ignoring message from {phone_number} as it's not a trigger or part of an ongoing follow-up.")

    return jsonify({"status": "success"}), 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)
