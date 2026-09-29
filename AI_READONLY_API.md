# AI read-only API

The following endpoints require `Authorization: Bearer <DRINKTHINK_AI_API_KEY>` and use only `DRINKTHINK_AI_MONGO_URL`, a separate read-only MongoDB credential.

- `GET /api/ai/health`
- `GET /api/ai/collections`
- `GET /api/ai/collections/{collection}` with `offset`, `limit` (1–100), and one exact `filter_field` / `filter_value` pair
- `GET /api/ai/collections/{collection}/{record_id}` where the collection has a canonical ID
- `GET /api/ai/collections/{collection}/count`
- `GET /api/ai/collections/{collection}/schema`

Only `cocktails`, `ingredients`, `ingredient_categories`, `glasses`, and `cocktail_ingredients` are accessible. No route accepts MongoDB operators, BSON query documents, database commands, or write methods.

## Railway variables

```text
DRINKTHINK_AI_API_KEY=<long-random-secret>
DRINKTHINK_AI_MONGO_URL=<read-only-MongoDB-URI>
DRINKTHINK_AI_DB_NAME=DrinkThinkv0
```

Codex needs only `DRINKTHINK_API_URL` and `DRINKTHINK_AI_API_KEY`; never provide it the MongoDB URI.
