from flask import Flask, render_template, abort

app = Flask(__name__)

@app.route('/map')
def index():
    return render_template('index.html')


# only allow camera1–camera4, but you can extend this list
VALID_CAMERAS = {'camera1', 'camera2', 'camera3', 'camera4', "zero1", "zero2"}

@app.route('/<camera_id>')
def camera_view(camera_id):
    if camera_id not in VALID_CAMERAS:
        abort(404)
    return render_template('cameras.html', camera_id=camera_id)

if __name__ == '__main__':
    # listen on 127.0.0.1:8000
    app.run(host='127.0.0.1', port=8000, debug=True)
