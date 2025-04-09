import os
import openai
from openai import OpenAI
import json
import time
import logging
from flask import Blueprint, render_template, request, jsonify,session,redirect,url_for,send_from_directory,current_app,abort
from openai import RateLimitError, OpenAIError
chatgpt_bp = Blueprint('chatgpt_bp', __name__)


# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
    current_user = session.get('username')
    return render_template('chatgpt.html',  current_user=current_user)  # Ensure 'chatgpt.html' is in your templates folder

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

    
# #stable fusion part
# from flask import Flask, render_template, request, send_file
# from diffusers import StableDiffusionPipeline
# import torch
# import uuid
# import threading
# generation_status = {}

# pipe = StableDiffusionPipeline.from_pretrained("runwayml/stable-diffusion-v1-5", torch_dtype=torch.float32)
# pipe.to("cuda")    
# # Define the output directory and prompts file
# OUTPUT_DIR = os.path.join("output", "stable")
# if not os.path.exists(OUTPUT_DIR):
#     os.makedirs(OUTPUT_DIR)
# PROMPTS_FILE = os.path.join(OUTPUT_DIR, "prompts.json")

# def load_images_data():
#     if os.path.exists(PROMPTS_FILE):
#         with open(PROMPTS_FILE, "r") as pf:
#             return json.load(pf)
#     return []

# def save_images_data(data):
#     with open(PROMPTS_FILE, "w") as pf:
#         json.dump(data, pf)

# def generate_image(prompt, steps, guidance, width, height):
#     global generation_status
#     generation_status['status'] = "Starting image generation..."
    
#     # Simulate image generation process
#     for i in range(1, steps + 1):
#         time.sleep(0.1)  # Simulate processing delay
#         generation_status['status'] = f"Generating... Step {i} of {steps}"

#     # Generate the image using the parameters
#     image = pipe(prompt,
#                  num_inference_steps=steps,
#                  guidance_scale=guidance,
#                  width=width,
#                  height=height).images[0]
    
#     filename = f"output_{uuid.uuid4().hex}.png"
#     image_path = os.path.join(OUTPUT_DIR, filename)
#     image.save(image_path)
    
#     # Update status and save image metadata
#     generation_status['status'] = "Image generation complete!"
    
#     image_data = {
#         'prompt': prompt,
#         'filename': filename,
#         'steps': steps,
#         'guidance': guidance,
#         'width': width,
#         'height': height
#     }
#     images_data = load_images_data()
#     images_data.append(image_data)
#     save_images_data(images_data)
    


# @chatgpt_bp.route('/stablediffusion', methods=['GET', 'POST'])
# def stablediffusion():
#     global generation_status
#     images_data = load_images_data()
#     if request.method == 'POST':
#         prompt = request.form['prompt']
#         steps = int(request.form.get('steps', 30))
#         guidance = float(request.form.get('guidance', 7.5))
#         width = int(request.form.get('width', 512))
#         height = int(request.form.get('height', 512))

#         # Start a new thread for image generation
#         thread = threading.Thread(target=generate_image, args=(prompt, steps, guidance, width, height))
#         thread.start()
#         current_app.logger.info(f"Image generation thread started for prompt: {prompt}")

#         return jsonify({'status': 'Image generation started.'}), 202

#     return render_template('stablediffusion.html', images_data=images_data)

# @chatgpt_bp.route('/output/stable/<filename>')
# def stable_output(filename):
#     # Ensure the requested file exists in the output directory
#     file_path = os.path.join(OUTPUT_DIR, filename)
    
#     if not os.path.exists(file_path):
#         abort(404)  # Return a 404 error if the file does not exist

#     # Return the file for download
#     return send_from_directory(OUTPUT_DIR, filename)
# @chatgpt_bp.route('/output/stable')
# def list_stable_outputs():
#     # List all files in the OUTPUT_DIR
#     files = os.listdir(OUTPUT_DIR)
    
#     # Filter files to include only .png files (or any other relevant extensions)
#     files = [f for f in files if f.endswith('.png')]
    
#     # Sort files by modification time (newest first)
#     files.sort(key=lambda x: os.path.getmtime(os.path.join(OUTPUT_DIR, x)), reverse=True)
    
#     # Return the sorted list of files (you can render this in a template or return as JSON)
#     return jsonify(files)

# @chatgpt_bp.route('/generation_status', methods=['GET'])
# def get_generation_status():
#     return jsonify(generation_status)