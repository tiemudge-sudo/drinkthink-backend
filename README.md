# DrinkThink Backend

Standalone FastAPI backend for the DrinkThink app. Owns the drinks database
(seeded into MongoDB from `data/drinks.json`) and serves the API the mobile
app talks to.

Split out from the original Emergent-generated monorepo so it can be run,
tested, and deployed independently of the frontend.

## Setup

1. Create a Python virtual environment (recommended):
   ```
   python -m venv venv
   venv\Scripts\activate      # Windows
   source venv/bin/activate   # Mac/Linux
   ```

2. Install dependencies:
   ```
   pip install -r requirements.txt
   pip install "pymongo[srv]"
   ```

3. Create a `.env` file in this folder with:
   ```
   MONGO_URL=mongodb+srv://<username>:<password>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
   DB_NAME=drinkthink
   ```
   (Use your own MongoDB Atlas connection string here — never commit this file.)

4. Run the server:
   ```
   uvicorn server:app --reload --port 8000
   ```

The drinks collection auto-seeds from `data/drinks.json` on startup if the
database is empty or out of sync with the JSON file's row count.

## Notes

- `emergentintegrations` was removed from requirements — it was listed but
  never actually imported/used in `server.py`, and it's a private package
  not available outside Emergent's platform.
- The frontend app (separate repo) points at this server via the
  `EXPO_PUBLIC_BACKEND_URL` environment variable, e.g.
  `http://localhost:8000` for local development.
