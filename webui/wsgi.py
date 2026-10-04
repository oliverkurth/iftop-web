"""WSGI entry point, also used by the Flask CLI: flask --app wsgi user add NAME"""

from web import create_app

app = create_app()
