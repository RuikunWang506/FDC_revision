from flask import Flask

app = Flask(__name__)

if __name__ == "__main__":
    try:
        app.run(host='0.0.0.0', port=5555, debug=True)
    except Exception as e:
        print(f"Error: {e}")
