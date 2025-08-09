import os
import threading
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

app = Flask(__name__)

# --- Configuration & API Clients ---
WASSENGER_API_URL = "https://api.wassenger.com/v1"
WASSENGER_API_KEY = os.getenv("WASSENGER_API_KEY")
OPENAI_CLIENT = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# In-memory storage for conversation context
# In production, replace this with a persistent database
follow_up_contacts = {}

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
    """Generates the first AI message and sends it after a delay."""
    try:
        initial_prompt = "You are a helpful medical assistant. A patient needs a follow-up. Please write a polite message to ask how they are doing after their recent appointment and if they have any questions. Keep it under 100 words."
        
        completion = OPENAI_CLIENT.chat.completions.create(
            model="gpt-4o", 
            messages=[{"role": "system", "content": initial_prompt}]
        )
        message_text = completion.choices[0].message.content

        send_message_to_wassenger(phone_number, message_text)
        
        if contact_id in follow_up_contacts:
            follow_up_contacts[contact_id]["status"] = "ongoing"
            follow_up_contacts[contact_id]["history"].append({"role": "assistant", "content": message_text})
        
        print(f"Initial follow-up sent to {phone_number}.")
    except Exception as e:
        print(f"Error sending follow-up to {phone_number}: {e}")

def handle_ai_reply(contact_id, phone_number, message_content):
    """Generates and sends an AI reply based on conversation history."""
    try:
        if contact_id not in follow_up_contacts:
            print(f"No ongoing conversation for {phone_number}. Skipping AI reply.")
            return

        follow_up_contacts[contact_id]["history"].append({"role": "user", "content": message_content})
        conversation_history = follow_up_contacts[contact_id]["history"]
        
        system_prompt = "You are a professional medical assistant replying to a patient. Be helpful, concise, and empathetic. Do not give medical advice. If the patient asks for an appointment or to speak with a doctor, tell them you will connect them with a human."
        
        messages = [{"role": "system", "content": system_prompt}] + conversation_history
        
        completion = OPENAI_CLIENT.chat.completions.create(
            model="gpt-4o",
            messages=messages
        )
        ai_reply = completion.choices[0].message.content
        
        send_message_to_wassenger(phone_number, ai_reply)
        
        follow_up_contacts[contact_id]["history"].append({"role": "assistant", "content": ai_reply})
        
        print(f"AI replied to {phone_number} with: {ai_reply}")
    except Exception as e:
        print(f"Error generating AI reply for {phone_number}: {e}")

# --- Webhook Endpoint ---
@app.route("/wassenger-webhook/", methods=["POST"])
def wassenger_webhook():
    """This is the main entry point for all webhook events from Wassenger."""
    payload = request.json
    event_type = payload.get("event")
    
    # Extract data from the payload, handling different event structures
    contact_id = payload.get("id") or payload.get("data", {}).get("contact", {}).get("id")
    phone_number = payload.get("data", {}).get("phone") or payload.get("data", {}).get("contact", {}).get("phone")
    
    if not phone_number or not contact_id:
        print("Webhook received with missing phone number or contact ID. Skipping.")
        return jsonify({"status": "error", "message": "Missing key data"}), 400

    # Handle the contact update event
    if event_type == "contact:update":
        labels = payload.get("data", {}).get("chat", {}).get("labels", [])
        
        if "Follow-up" in labels and contact_id not in follow_up_contacts:
            print(f"Follow-up label detected for {phone_number}. Scheduling message.")
            
            follow_up_contacts[contact_id] = {
                "status": "scheduled",
                "history": [],
                "phone_number": phone_number
            }
            threading.Timer(86400, send_initial_follow_up, args=[contact_id, phone_number]).start()
            
            return jsonify({"status": "success", "message": "Follow-up scheduled"}), 200

    # Handle an incoming message from a patient
    elif event_type == "message:in:new":
        message_data = payload.get("data", {})
        message_body = message_data.get("content", "").strip()
        
        # Check for the trigger keyword
        if message_data.get("fromMe") is True and message_body.upper() == "START FOLLOWUP":
            print(f"Message trigger detected for {phone_number}. Scheduling message.")
            
            if contact_id not in follow_up_contacts:
                follow_up_contacts[contact_id] = {
                    "status": "scheduled",
                    "history": [],
                    "phone_number": phone_number
                }
            
            threading.Timer(86400, send_initial_follow_up, args=[contact_id, phone_number]).start()
            
            return jsonify({"status": "success", "message": "Follow-up scheduled"}), 200

        # Handle a regular patient reply if an ongoing conversation exists
        elif message_data.get("fromMe") is False and contact_id in follow_up_contacts and follow_up_contacts[contact_id]["status"] == "ongoing":
            print(f"Patient {phone_number} replied: {message_body}")
            threading.Thread(target=handle_ai_reply, args=[contact_id, phone_number, message_body]).start()

        else:
            print(f"Ignoring message from {phone_number} as it's not a trigger or part of an ongoing follow-up.")

    return jsonify({"status": "success"}), 200

# This is for local development only. Gunicorn will handle this in production on Render.
if __name__ == "__main__":
    app.run(port=5000, debug=True)
