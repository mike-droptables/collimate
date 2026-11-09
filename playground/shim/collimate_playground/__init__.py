"""collimate_playground — the in-browser (pyodide) shim around the real app.

Imported only inside the pyodide runtime the playground build assembles;
nothing under api/ or frontend/ knows it exists. ``boot.boot()`` is the one
entry point: it installs the threadless + stub-module patches, imports the
staged ``main`` module, runs the app's lifespan, seeds the single demo
channel through the app's own routes and returns the request bridges the
page uses (``boot.http`` / ``boot.ws_connect``).
"""
