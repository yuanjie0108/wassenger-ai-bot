import os
import threading
import requests
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, timedelta
import time

# Load environment variables from the .env file
load_dotenv()

app = Flask(__name__)

# --- Configuration & API Clients ---
WASSENGER_API_URL = os.getenv("WASSENGER_API_URL", "https://api.wassenger.com/v1")
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
    """Sends a message to a specific phone number via the Wassenger API."""
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
    """Generates the first AI message and sends it."""
    try:
        initial_prompt = "You are a helpful medical assistant. A patient needs a follow-up. Please write a polite message to ask how they are doing after their recent appointment and if they have any questions. Keep it under 100 words."
        
        completion = OPENAI_CLIENT.chat.completions.create(
            model="gpt-4o", 
            messages=[{"role": "system", "content": initial_prompt}]
        )
        message_text = completion.choices[0].message.content

        send_message_to_wassenger(phone_number, message_text)
        print(f"AI initial follow-up message sent to {phone_number}.")
        
        # Update database
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT history FROM follow_ups WHERE contact_id = %s", (contact_id,))
        result = cursor.fetchone()
        
        if result:
            history = result[0]
            history.append({"role": "assistant", "content": message_text})
            cursor.execute("UPDATE follow_ups SET status = %s, history = %s WHERE contact_id = %s", ('ongoing', psycopg2.extras.Json(history), contact_id))
        else:
            print(f"Warning: No follow-up found for {contact_id}. Initial message sent, but conversation status not updated.")
            
        conn.commit()
        cursor.close()
        conn.close()
        
        print(f"Initial follow-up sent to {phone_number}.")
    except Exception as e:
        print(f"Error sending follow-up to {phone_number}: {e}")

def handle_ai_reply(contact_id, phone_number, message_content):
    """Generates and sends an AI reply based on conversation history."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT status, history FROM follow_ups WHERE contact_id = %s", (contact_id,))
        result = cursor.fetchone()
        
        if not result or result[0] != 'ongoing':
            conn.close()
            print(f"No ongoing conversation for {phone_number}. Skipping AI reply.")
            return

        status, history = result
        history.append({"role": "user", "content": message_content})
        
        system_prompt = "You are a professional medical assistant replying to a patient. Be helpful, concise, and empathetic. Do not give medical advice.
