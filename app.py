import os
import time
import threading
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from dotenv import load_dotenv

# Load environment variables from the .env file.
# This makes your API keys available to the application.
load_dotenv()

app = Flask(__name__)

# --- Configuration & API Clients ---
# These variables hold your API endpoints and keys, retrieved from the .env file.
# It's crucial to retrieve them securely this way.
WASSENGER_API_URL = "https://api.wassenger.com/v1"
WASSENGER_API_KEY = os.getenv("WASSENGER_API_KEY")
OPENAI_CLIENT = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# In-memory storage for conversation context.
# In a real-world production app, you would replace this with a database (like PostgreSQL or MongoDB)
# to ensure data persists even if the server restarts.
follow_up_contacts = {}

# --- Helper Functions ---
# These functions abstract the logic for communicating with external services.

def send_message_to_wassenger(phone, message_content):
    """Sends a message to a specific phone number via the Wassenger API."""
    headers = {"Authorization": f"Bearer {WASSENGER_API_KEY}", "Content-Type": "application/json"}
    payload = {"phone": phone, "message": message_content}
    try:
        response = requests.post(f"{WASSENGER_API_URL}/messages", json=payload, headers=headers)
        response.raise_for_status()  # This will raise an HTTPError for bad responses (4xx or 5xx)
        print(f"Message sent successfully to {phone}.")
    except requests.exceptions.HTTPError as err:
        print(f"HTTP Error: {err.response.text}")
    except Exception as e:
        print(f"An error occurred while sending message: {e}")

def send_initial_follow_up(contact_id, phone_number):
    """
    Called by a scheduled timer. Generates the first AI message and sends it.
    """
    try:
        initial_prompt = "You are a helpful medical assistant. A patient needs a follow-up. Please write a polite message to ask how they are doing after their recent appointment and if they have any questions. Keep it under 100 words."
        
        completion = OPENAI_CLIENT.chat.completions.create(
            model="gpt-4o", 
            messages=[{"role": "system", "content": initial_prompt}]
        )
        message_text = completion.choices[0].message.content

        send_message_to_wassenger(phone_number, message_text)
        
        # Update the contact's status and store the initial message history
        if contact_id in follow_up_contacts:
            follow_up_contacts[contact_id]["status"] = "ongoing"
            follow_up_contacts[contact_id]["history"].append({"role": "assistant", "content": message_text})
        
        print(f"Initial follow-up sent to {phone_number}.")
    except Exception as e:
        print(f"Error sending follow-up to {phone_number}: {e}")

def handle_ai_reply(contact_id, phone_number, message_content):
    """
    Generates an AI reply based on conversation history and sends it.
    Runs in a separate thread to avoid blocking the webhook.
    """
    try:
        # Get the conversation history and append the new message
        if contact_id in follow_up_contacts:
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
            
            # Update history with the AI's reply
            follow_up_contacts[contact_id]["history"].append({"role": "assistant", "content": ai_reply})
            
            print(f"AI replied to {phone_number} with: {ai_reply}")
    except Exception as e:
        print(f"Error generating AI reply for {phone_number}: {e}")

# --- Webhook Endpoint ---
@app.route("/wassenger-webhook", methods=["POST"])
def wassenger_webhook():
    """This is the main entry point for all webhook events from Wassenger."""
    payload = request.json
    event_type = payload.get("event")
    contact_id = payload.get("data", {}).get("contact", {}).get("id")
    phone_number = payload.get("data", {}).get("contact", {}).get("phone")
    
    if not phone_number or not contact_id:
        return jsonify({"error": "Missing phone number or contact ID"}), 400

    # Handle a contact being labeled
    if event_type == "contact.update":
        labels = payload.get("data", {}).get("labels", [])
        if "Follow-up" in labels and contact_id not in follow_up_contacts:
            print(f"Follow-up label detected for {phone_number}. Scheduling message.")
            
            # Store the contact and schedule the initial message for 24 hours later
            follow_up_contacts[contact_id] = {
                "status": "scheduled",
                "history": [],
                "phone_number": phone_number
            }
            # The threading.Timer will run send_initial_follow_up after 86400 seconds (24 hours)
            threading.Timer(86400, send_initial_follow_up, args=[contact_id, phone_number]).start()
            
    # Handle an incoming message from a patient
    elif event_type == "messages.upsert":
        message_data = payload.get("data")
        if not message_data.get("fromMe") and contact_id in follow_up_contacts and follow_up_contacts[contact_id]["status"] == "ongoing":
            message_body = message_data.get("content")
            print(f"Patient {phone_number} replied: {message_body}")
            
            # Handle the AI response in a separate thread to prevent timeouts on the webhook
            threading.Thread(target=handle_ai_reply, args=[contact_id, phone_number, message_body]).start()

    return jsonify({"status": "success"}), 200

# This is for local development only. Gunicorn will handle this in production on Render.
if __name__ == "__main__":
    app.run(port=5000, debug=True)
