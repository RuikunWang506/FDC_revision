import os
import openai
from openai import OpenAI
import json
import time
import logging
from flask import Blueprint, render_template, request, jsonify,session,redirect,url_for
from openai import RateLimitError, OpenAIError

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

chatgpt_bp = Blueprint('chatgpt_bp', __name__)

def load_credentials():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    credentials_path = os.path.join(base_dir, 'credentials.json')
    if not os.path.exists(credentials_path):
        logger.error(f"Credentials file not found at {credentials_path}")
        raise FileNotFoundError(f"Credentials file not found at {credentials_path}")
    with open(credentials_path) as file:
        credentials = json.load(file)
    return credentials

def setup_openai():
    credentials = load_credentials()
    api_key = credentials.get('openai_api_key')
    if not api_key:
        logger.error("OpenAI API key not found in credentials.")
        raise ValueError("OpenAI API key not found in credentials.")
    openai.api_key = api_key

# Set up OpenAI API key
setup_openai()
client = OpenAI()
def get_gpt_response(message, retries=5, backoff_factor=2):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "user", "content": message},
            ],
            timeout=10
        )
        reply = response.choices[0].message.content

        return reply
    except RateLimitError as e:
        if retries > 0:
            delay = backoff_factor ** (5 - retries)
            logger.warning(f"Rate limit exceeded. Retrying in {delay} seconds...")
            time.sleep(delay)
            return get_gpt_response(message, retries - 1, backoff_factor)
        else:
            logger.error("Rate limit exceeded after multiple retries.")
            return "Error: Rate limit exceeded. Please try again later."
    except OpenAIError as e:
        logger.error(f"OpenAI API error: {e}")
        return f"An OpenAI error occurred: {str(e)}"
    except Exception as e:
        logger.exception("An unexpected error occurred.")
        return f"An error occurred: {str(e)}"

@chatgpt_bp.route('/chatgpt', methods=['GET'])
def chat_page():
    if 'username' not in session:
            return redirect(url_for("login"))
    """Serve the ChatGPT interface page."""
    return render_template('chatgpt.html')  # Ensure 'chatgpt.html' is in your templates folder

@chatgpt_bp.route('/chatgpt', methods=['POST'])
def chat():
    """Handle chat messages sent via POST requests."""
    try:
        data = request.get_json()
        if not data:
            logger.warning("No JSON payload received.")
            return jsonify({'response': 'Invalid request. JSON data expected.'}), 400

        user_message = data.get('message', '').strip()
        if not user_message:
            logger.warning("Empty message received.")
            return jsonify({'response': 'Please enter a message.'}), 400

        logger.info(f"Received message: {user_message}")
        gpt_response = get_gpt_response(user_message)
        logger.info(f"GPT response: {gpt_response}")
        return jsonify({'response': gpt_response}), 200
    except Exception as e:
        logger.exception("Error handling /chatgpt request.")
        return jsonify({'response': f"An internal error occurred: {str(e)}"}), 500
