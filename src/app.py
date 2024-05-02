from flask import Flask, request, jsonify
import ocr_service

def create_app():
    app = Flask(__name__)

    @app.route('/ocr', methods=['POST'])
    def ocr():
        file = request.files['file']
        model_name = request.form.get('model', None)
        text = ocr_service.extract_text(file, model_name=model_name)                
        return jsonify({'text': text})

    @app.route('/models', methods=['GET'])
    def list_models():
        models = ocr_service.get_supported_models()
        return jsonify({'supported_models': models})

    @app.errorhandler(400)
    def bad_request(error):
        return jsonify({'error': 'Bad request'}), 400

    @app.errorhandler(500)
    def internal_server_error(error):
        return jsonify({'error': 'Internal server error'}), 500

    return app

app = create_app()
