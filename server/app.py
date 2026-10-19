import logging
from flask import Flask, request
from server.config import config, AppConfig
from server.database import init_db
from server.views.error_view import make_error_response
from server.controllers.health_controller import health_bp

def create_app(custom_config: AppConfig = None) -> Flask:
    """Application factory for the Roshta Flask service."""
    app = Flask(__name__)
    active_config = custom_config or config
    app.config["APP_CONFIG"] = active_config

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s"
    )

    # Initialize SQLite database with WAL mode
    init_db(active_config.database_path)

    # Register blueprints
    app.register_blueprint(health_bp)

    # Token Authentication Middleware
    @app.before_request
    def require_token():
        # Allow /health without authentication for service discovery & vitality checks
        if request.path == "/health":
            return None

        # Allow OPTIONS for CORS preflight
        if request.method == "OPTIONS":
            return None

        token = request.headers.get("X-Token")
        if not token or token != active_config.secret_token:
            return make_error_response(
                code="UNAUTHORIZED",
                message="Missing or invalid X-Token header.",
                status_code=401
            )

    # Standard error handlers
    @app.errorhandler(404)
    def handle_not_found(e):
        return make_error_response("NOT_FOUND", "The requested endpoint does not exist.", 404)

    @app.errorhandler(405)
    def handle_method_not_allowed(e):
        return make_error_response("METHOD_NOT_ALLOWED", "Method not allowed for this route.", 405)

    @app.errorhandler(Exception)
    def handle_internal_error(e):
        app.logger.exception("Unhandled server exception:")
        return make_error_response("INTERNAL_ERROR", str(e), 500)

    return app

def main():
    """Runs the service using the production Waitress WSGI server."""
    from waitress import serve
    app = create_app()
    print(f"=====================================================")
    print(f"  Roshta Print Automation Service v2.0")
    print(f"  Single Source of Truth: SQLite WAL ({config.database_path})")
    print(f"  Binding to http://{config.host}:{config.port}")
    print(f"  Waitress WSGI Server Running...")
    print(f"=====================================================")
    serve(app, host=config.host, port=config.port, threads=4)

if __name__ == "__main__":
    main()
