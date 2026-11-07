# Raccourci : permet aussi de démarrer avec "gunicorn app:app" ou "python app.py"
import os
from web_app import app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
