from flask import jsonify, Response

def make_error_response(code: str, message: str, status_code: int = 400) -> Response:
    """Creates a consistent JSON error envelope adhering to the API specification."""
    payload = {
        "error": {
            "code": code,
            "message": message
        }
    }
    response = jsonify(payload)
    response.status_code = status_code
    return response
