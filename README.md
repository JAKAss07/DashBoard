## Requisitos

- [uv](https://docs.astral.sh/uv/) (instala Python 3.12 automáticamente)
- Una base de datos PostgreSQL con la tabla `logs`

## Instalación

```
uv sync
```

## Configuración

1. Copia `.env.example` como `.env`.
2. Completa `DATABASE_URL` con tus datos:

## Correr el dashboard

```
uv run python app.py
```

Abre en el navegador: http://127.0.0.1:8050
