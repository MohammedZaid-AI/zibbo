# Items API

A small FastAPI service for managing items and their owners.

## Features

- CRUD endpoints for items
- Pagination on list endpoints
- Pydantic request/response models
- OpenAPI schema at `/openapi.json`

## Quick start

```bash
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs for the interactive API.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/items` | List items (supports `skip` and `limit`) |
| POST | `/items` | Create an item |
| GET | `/items/{id}` | Fetch one item |
| DELETE | `/items/{id}` | Delete an item |

## Testing

```bash
pytest
```

## License

MIT.
