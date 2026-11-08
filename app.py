import os
import requests
from flask import Flask, request, make_response
from flask_cors import CORS, cross_origin
import requests_cache

from dotenv import load_dotenv
load_dotenv("local.env")

from index_api import bp as index_bp

app = Flask('__main__')
cors = CORS(app)
# optional /explorer/... routes, answer 503 until indexer.py has built its database
app.register_blueprint(index_bp)
method_requests_mapping = {
    'GET': requests.get,
    'HEAD': requests.head,
    'POST': requests.post,
    'PUT': requests.put,
    'DELETE': requests.delete,
    'PATCH': requests.patch,
    'OPTIONS': requests.options,
}
SERVER_URL = os.getenv("SERVER_URL").rstrip('/')
AUTH_TOKEN = os.getenv("AUTH_TOKEN")

# install cache provider for requests
requests_cache.install_cache(
    'nexusapi_cache', backend='sqlite', expire_after=5)


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>', methods=method_requests_mapping.keys())
@cross_origin()
def proxy(path):
    """Proxy requests to the Nexus Core."""
    if request.method == "OPTIONS":  # CORS preflight
        return _build_cors_preflight_response()
    else:
        resp = requests.request(method=request.method,
                                url=f'{SERVER_URL}/{path}',
                                headers=request.headers,
                                params=request.args,
                                data=request.data,
                                json=request.get_json(silent=True))
        return _corsify_actual_response(resp)


def _build_cors_preflight_response():
    response = make_response()
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add('Access-Control-Allow-Headers', "*")
    response.headers.add('Access-Control-Allow-Methods', "*")
    return response


def _corsify_actual_response(response):
    # keep the node's status code so clients can tell errors (e.g. not found) from results
    try:
        body = response.json()
    except ValueError:
        body = {"error": {"code": response.status_code, "message": response.text[:500]}}
    # flask-cors adds Access-Control-Allow-Origin itself
    return make_response(body, response.status_code)


def main():
    """Start the application."""
    app.run(host='0.0.0.0', port=8080)


if __name__ == "__main__":
    main()
