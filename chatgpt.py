import tempfile
import base64
import os
from urllib.parse import urlparse
import json
import logging
from openai import OpenAI
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for,current_app

chatgpt_bp = Blueprint('chatgpt_bp', __name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AVAILABLE_MODELS = {
    "nvidia/llama-3.3-nemotron-super-49b-v1:free": {"name": "Llama 3.3 Nemotron Super (free, not very fast)", "attachments": True},
    "gpt-4o-mini": {"name": "GPT-4o Mini ($0.15/1M words, try not to use it too much)", "attachments": False},
    "openrouter/quasar-alpha": {"name": "quasar-alpha (free, this is probly from openAI)", "attachments": "image"}
}

base_dir = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(base_dir, 'credentials.json')) as file:
    credentials = json.load(file)

openai_client = OpenAI(api_key=credentials['openai_api_key'])
openrouter_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=credentials['openrouter_api_key']
)

def get_gpt_response(message, model_name, attachment=None):
    if model_name == "gpt-4o-mini":
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": message}],
            timeout=10
        )
        return response.choices[0].message.content
    else:
        if attachment:
            # Handle the complete base64 string as one unit
            message_content = [
                {"type": "text", "text": message},
                {
                    "type": "image_url",
                    "image_url": {"url": attachment}  # Use complete base64 string
                }
            ]

        else:
            message_content = message

        response = openrouter_client.chat.completions.create(
            extra_headers={
                "HTTP-Referer": "https://fdcef.com",
                "X-Title": "FDC App"
            },
            model=model_name,
            messages=[{
                "role": "user", 
                "content": message_content
            }]
        )
        current_app.logger.info(f"message_content: {message_content}")

        return response.choices[0].message.content

@chatgpt_bp.route('/chatgpt', methods=['GET'])
def chat_page():
    if 'username' not in session:
        return redirect(url_for("login"))
    return render_template('chatgpt.html', 
                         current_user=session.get('username'),
                         models=AVAILABLE_MODELS)

@chatgpt_bp.route('/chatgpt', methods=['POST'])
def chat():
    data = request.get_json()
    message = data.get('message', '').strip()
    model = data.get('model', 'gpt-4o-mini')
    attachment = data.get('attachment')

    if not message:
        return jsonify({'response': 'Please enter a message.'}), 400

    current_app.logger.info(f"Received message: {message} for model: {model}")
    gpt_response = get_gpt_response(message, model, attachment)
    return jsonify({'response': gpt_response}), 200