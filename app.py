import os
import uuid
import json
from datetime import datetime
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_socketio import SocketIO, join_room, leave_room, emit, close_room
from werkzeug.utils import secure_filename

# ----------------------------
# تنظیمات پایه
# ----------------------------
app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_secret_key_change_in_production')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload

UPLOAD_DIR = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_IMAGE_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_AUDIO_EXT = {'wav', 'mp3', 'ogg', 'webm'}
ALLOWED_FILE_EXT = {'pdf', 'txt', 'doc', 'docx'}

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

# ----------------------------
# داده‌های درون‌حافظه (در تولید از دیتابیس استفاده کن)
# ----------------------------
rooms = {}  # room_id -> {users: [], messages: [], is_private: bool}
users_in_room = {}  # room_id -> [{username, sid, display_name}]
active_calls = {}  # room_id -> {caller: username, type: 'audio/video', participants: []}

# حساب‌های اولیه
accounts = {
    "yasin": {"password": "yasin.7734", "display_name": "یاسین", "is_admin": True},
    "leila": {"password": "1365", "display_name": "لیلا", "is_admin": False},
    "zeynab": {"password": "1362", "display_name": "زینب", "is_admin": False},
    "tasnim": {"password": "1388", "display_name": "تسنیم", "is_admin": False},
}

# نگهداری پیام‌ها برای امکان ویرایش/حذف
# ساختار: message_id -> {username, message, timestamp, room, edited, deleted}
all_messages = {}

# تم پیش‌فرض کاربران
user_themes = {}

# ----------------------------
# توابع کمکی
# ----------------------------
def allowed_file(filename, file_type='image'):
    if '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    if file_type == 'image':
        return ext in ALLOWED_IMAGE_EXT
    elif file_type == 'audio':
        return ext in ALLOWED_AUDIO_EXT
    elif file_type == 'file':
        return ext in ALLOWED_FILE_EXT
    return False

def get_user_display_name(username):
    return accounts.get(username, {}).get('display_name', username)

def create_private_room_id(user1, user2):
    """ایجاد شناسه یکتا برای چت خصوصی"""
    users = sorted([user1, user2])
    return f'private_{users[0]}_{users[1]}'

def is_user_admin(username):
    return accounts.get(username, {}).get('is_admin', False)

# اتاق عمومی
GLOBAL_ROOM = 'global_chat_room'
rooms[GLOBAL_ROOM] = {
    'users': [],
    'messages': [],
    'is_private': False,
    'created_at': datetime.now().isoformat()
}

# ----------------------------
# مسیرهای وب
# ----------------------------
@app.route('/')
def index():
    if 'username' not in session:
        return redirect(url_for('login'))
    
    username = session['username']
    display_name = get_user_display_name(username)
    is_admin = is_user_admin(username)
    theme = user_themes.get(username, 'light')
    
    # لیست کاربران آنلاین
    online_users = []
    for room_data in users_in_room.values():
        for user in room_data:
            if user['username'] not in [u['username'] for u in online_users]:
                online_users.append({
                    'username': user['username'],
                    'display_name': get_user_display_name(user['username'])
                })
    
    # لیست چت‌های خصوصی کاربر
    private_chats = []
    for room_id, room_data in rooms.items():
        if room_data['is_private'] and username in room_id:
            # پیدا کردن کاربر دیگر در چت خصوصی
            parts = room_id.split('_')
            other_user = parts[2] if parts[1] == username else parts[1]
            private_chats.append({
                'room_id': room_id,
                'other_user': other_user,
                'display_name': get_user_display_name(other_user),
                'last_message': room_data['messages'][-1] if room_data['messages'] else None
            })
    
    return render_template('index.html',
                         username=username,
                         display_name=display_name,
                         is_admin=is_admin,
                         theme=theme,
                         online_users=online_users,
                         private_chats=private_chats)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('login.html')
    
    data = request.form
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    
    if not username or not password:
        return render_template('login.html', error="نام کاربری و رمز عبور را وارد کنید")
    
    acct = accounts.get(username)
    if acct and acct.get('password') == password:
        session['username'] = username
        return redirect(url_for('index'))
    
    return render_template('login.html', error="ورود ناموفق بود. حساب وجود ندارد یا رمز اشتباه است.")

@app.route('/logout', methods=['POST'])
def logout():
    username = session.pop('username', None)
    if username:
        # اطلاع به تمام اتاق‌ها
        for room_id in list(users_in_room.keys()):
            if room_id in users_in_room:
                users_in_room[room_id] = [u for u in users_in_room[room_id] if u['username'] != username]
        
        socketio.emit('user_offline', {'username': username}, broadcast=True)
    
    return redirect(url_for('login'))

@app.route('/api/admin/create_user', methods=['POST'])
def admin_create_user():
    if 'username' not in session:
        return jsonify({'ok': False, 'error': 'نیاز به ورود است'}), 401
    
    username = session['username']
    if not is_user_admin(username):
        return jsonify({'ok': False, 'error': 'دسترسی ادمین نیاز است'}), 403
    
    data = request.json or {}
    new_user = data.get('username', '').strip()
    new_pass = data.get('password', '').strip()
    display_name = data.get('display_name', '').strip() or new_user
    
    if not new_user or not new_pass:
        return jsonify({'ok': False, 'error': 'نام کاربری و رمز عبور الزامی است'}), 400
    
    if new_user in accounts:
        return jsonify({'ok': False, 'error': 'این نام کاربری از قبل وجود دارد'}), 400
    
    accounts[new_user] = {
        'password': new_pass,
        'display_name': display_name,
        'is_admin': data.get('is_admin', False)
    }
    
    return jsonify({'ok': True, 'message': 'حساب کاربری ایجاد شد'})

@app.route('/api/user/update_theme', methods=['POST'])
def update_user_theme():
    if 'username' not in session:
        return jsonify({'ok': False, 'error': 'نیاز به ورود است'}), 401
    
    data = request.json or {}
    theme = data.get('theme', 'light')
    
    if theme not in ['light', 'dark']:
        return jsonify({'ok': False, 'error': 'تم نامعتبر'}), 400
    
    username = session['username']
    user_themes[username] = theme
    
    return jsonify({'ok': True, 'theme': theme})

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'username' not in session:
        return jsonify({'ok': False, 'error': 'ابتدا وارد شوید'}), 403
    
    if 'file' not in request.files:
        return jsonify({'ok': False, 'error': 'فایلی ارسال نشده'}), 400
    
    f = request.files['file']
    filename = secure_filename(f.filename)
    
    if not filename:
        return jsonify({'ok': False, 'error': 'نام فایل نامعتبر'}), 400
    
    # تشخیص نوع فایل
    ext = filename.split('.')[-1].lower()
    
    if ext in ALLOWED_IMAGE_EXT:
        file_type = 'image'
    elif ext in ALLOWED_AUDIO_EXT:
        file_type = 'audio'
    elif ext in ALLOWED_FILE_EXT:
        file_type = 'file'
    else:
        return jsonify({'ok': False, 'error': 'پسوند فایل پشتیبانی نمی‌شود'}), 400
    
    unique = f"{uuid.uuid4().hex}.{ext}"
    save_path = os.path.join(UPLOAD_DIR, unique)
    
    try:
        f.save(save_path)
        file_url = url_for('static', filename=f'uploads/{unique}', _external=False)
        
        return jsonify({
            'ok': True,
            'url': file_url,
            'type': file_type,
            'filename': filename
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/messages/<message_id>', methods=['DELETE', 'PUT'])
def handle_message(message_id):
    if 'username' not in session:
        return jsonify({'ok': False, 'error': 'نیاز به ورود است'}), 401
    
    username = session['username']
    
    if message_id not in all_messages:
        return jsonify({'ok': False, 'error': 'پیام پیدا نشد'}), 404
    
    message_data = all_messages[message_id]
    
    if message_data['username'] != username:
        return jsonify({'ok': False, 'error': 'شما اجازه ویرایش/حذف این پیام را ندارید'}), 403
    
    if request.method == 'DELETE':
        # حذف منطقی پیام
        message_data['deleted'] = True
        message_data['deleted_at'] = datetime.now().isoformat()
        
        # اطلاع به کاربران در اتاق
        socketio.emit('message_deleted', {
            'message_id': message_id,
            'room': message_data['room']
        }, room=message_data['room'])
        
        return jsonify({'ok': True, 'message': 'پیام حذف شد'})
    
    elif request.method == 'PUT':
        # ویرایش پیام
        data = request.json or {}
        new_content = data.get('content', '').strip()
        
        if not new_content:
            return jsonify({'ok': False, 'error': 'متن پیام نمی‌تواند خالی باشد'}), 400
        
        message_data['message'] = new_content
        message_data['edited'] = True
        message_data['edited_at'] = datetime.now().isoformat()
        
        # اطلاع به کاربران در اتاق
        socketio.emit('message_edited', {
            'message_id': message_id,
            'content': new_content,
            'room': message_data['room']
        }, room=message_data['room'])
        
        return jsonify({'ok': True, 'message': 'پیام ویرایش شد'})

# ----------------------------
# رویدادهای Socket.IO
# ----------------------------
@socketio.on('connect')
def handle_connect():
    username = session.get('username')
    if username:
        emit('user_online', {'username': username}, broadcast=True)

@socketio.on('join')
def on_join(data):
    username = data.get('username')
    room = data.get('room', GLOBAL_ROOM)
    is_private = data.get('is_private', False)
    
    if not username:
        return
    
    # اگر چت خصوصی است، اتاق را ایجاد کن
    if is_private and room not in rooms:
        other_user = data.get('other_user')
        if not other_user:
            return
        
        rooms[room] = {
            'users': [],
            'messages': [],
            'is_private': True,
            'participants': [username, other_user],
            'created_at': datetime.now().isoformat()
        }
    
    join_room(room)
    
    # اضافه کردن کاربر به لیست کاربران اتاق
    if room not in users_in_room:
        users_in_room[room] = []
    
    user_info = {
        'username': username,
        'sid': request.sid,
        'display_name': get_user_display_name(username),
        'joined_at': datetime.now().isoformat()
    }
    
    # اگر کاربر قبلاً در اتاق نبوده
    if not any(u['username'] == username for u in users_in_room[room]):
        users_in_room[room].append(user_info)
        
        # اطلاع به دیگران
        emit('user_joined', {
            'username': username,
            'display_name': user_info['display_name'],
            'room': room
        }, room=room)
    
    # ارسال لیست کاربران به کاربر جدید
    emit('room_users', {
        'users': users_in_room[room],
        'room': room
    })
    
    # ارسال تاریخچه پیام‌ها
    if room in rooms:
        emit('message_history', {
            'messages': rooms[room]['messages'][-100:],  # آخرین 100 پیام
            'room': room
        })

@socketio.on('leave')
def on_leave(data):
    username = data.get('username')
    room = data.get('room', GLOBAL_ROOM)
    
    if not username or room not in users_in_room:
        return
    
    # حذف کاربر از لیست
    users_in_room[room] = [u for u in users_in_room[room] if u['username'] != username]
    
    leave_room(room)
    
    # اگر چت خصوصی است و هیچ کاربری نمانده، اتاق را ببند
    if rooms.get(room, {}).get('is_private', False) and not users_in_room[room]:
        if room in rooms:
            del rooms[room]
        if room in users_in_room:
            del users_in_room[room]
    
    # اطلاع به دیگران
    emit('user_left', {
        'username': username,
        'room': room
    }, room=room)

@socketio.on('send_message')
def handle_message(data):
    username = data.get('username')
    room = data.get('room', GLOBAL_ROOM)
    message = data.get('message', '').strip()
    reply_to = data.get('reply_to')
    message_type = data.get('type', 'text')
    
    if not username or not message:
        return
    
    # ایجاد شناسه یکتا برای پیام
    message_id = str(uuid.uuid4())
    timestamp = datetime.now().isoformat()
    
    message_obj = {
        'id': message_id,
        'username': username,
        'display_name': get_user_display_name(username),
        'message': message,
        'type': message_type,
        'room': room,
        'timestamp': timestamp,
        'reply_to': reply_to,
        'edited': False,
        'deleted': False
    }
    
    # ذخیره در تاریخچه اتاق
    if room not in rooms:
        rooms[room] = {'users': [], 'messages': [], 'is_private': False}
    
    rooms[room]['messages'].append(message_obj)
    
    # ذخیره در دیکشنری پیام‌ها برای امکان ویرایش/حذف
    all_messages[message_id] = {
        'username': username,
        'message': message,
        'timestamp': timestamp,
        'room': room,
        'edited': False,
        'deleted': False
    }
    
    # ارسال به همه در اتاق
    emit('new_message', message_obj, room=room)
    
    # ارسال نوتیفیکیشن به کاربران آنلاین (به جز فرستنده)
    for user in users_in_room.get(room, []):
        if user['username'] != username:
            emit('notification', {
                'title': 'پیام جدید',
                'body': f'{message_obj["display_name"]}: {message[:50]}...',
                'room': room,
                'from': username
            }, room=user['sid'])

@socketio.on('typing')
def handle_typing(data):
    room = data.get('room', GLOBAL_ROOM)
    username = data.get('username')
    is_typing = data.get('is_typing', False)
    
    emit('user_typing', {
        'username': username,
        'display_name': get_user_display_name(username),
        'is_typing': is_typing,
        'room': room
    }, room=room, include_self=False)

@socketio.on('start_call')
def handle_start_call(data):
    caller = data.get('caller')
    room = data.get('room', GLOBAL_ROOM)
    call_type = data.get('type', 'audio')  # audio or video
    
    if not caller or room not in users_in_room:
        return
    
    # ذخیره اطلاعات تماس
    active_calls[room] = {
        'caller': caller,
        'type': call_type,
        'participants': [caller],
        'started_at': datetime.now().isoformat()
    }
    
    # ارسال زنگ به همه در اتاق (به جز تماس‌گیرنده)
    for user in users_in_room[room]:
        if user['username'] != caller:
            emit('incoming_call', {
                'caller': caller,
                'caller_name': get_user_display_name(caller),
                'room': room,
                'type': call_type
            }, room=user['sid'])

@socketio.on('answer_call')
def handle_answer_call(data):
    answerer = data.get('answerer')
    room = data.get('room')
    accept = data.get('accept', False)
    
    if not answerer or room not in active_calls:
        return
    
    if accept:
        # اضافه کردن پاسخ‌دهنده به لیست شرکت‌کنندگان
        if answerer not in active_calls[room]['participants']:
            active_calls[room]['participants'].append(answerer)
        
        # اطلاع به تماس‌گیرنده
        emit('call_accepted', {
            'answerer': answerer,
            'answerer_name': get_user_display_name(answerer)
        }, room=room)
    else:
        # رد تماس
        emit('call_rejected', {
            'answerer': answerer,
            'answerer_name': get_user_display_name(answerer)
        }, room=room)
        
        # اگر هیچکس پاسخ نداد، تماس را قطع کن
        if len(active_calls[room]['participants']) == 1:
            end_call_in_room(room)

@socketio.on('end_call')
def handle_end_call(data):
    username = data.get('username')
    room = data.get('room')
    
    end_call_in_room(room, username)

def end_call_in_room(room, ended_by=None):
    if room in active_calls:
        call_data = active_calls.pop(room)
        
        emit('call_ended', {
            'ended_by': ended_by,
            'ended_by_name': get_user_display_name(ended_by) if ended_by else 'سیستم',
            'duration': '...'  # می‌تونی مدت زمان را محاسبه کنی
        }, room=room)

@socketio.on('rtc_signal')
def handle_rtc_signal(data):
    """رساندن سیگنال WebRTC به کاربر مقصد"""
    to_user = data.get('to')
    signal = data.get('signal')
    signal_type = data.get('type')  # offer, answer, candidate
    
    if not to_user or not signal:
        return
    
    # پیدا کردن sid کاربر مقصد
    target_sid = None
    for room_users in users_in_room.values():
        for user in room_users:
            if user['username'] == to_user:
                target_sid = user['sid']
                break
        if target_sid:
            break
    
    if target_sid:
        emit('rtc_signal', {
            'from': session.get('username'),
            'signal': signal,
            'type': signal_type
        }, room=target_sid)

@socketio.on('disconnect')
def handle_disconnect():
    username = session.get('username')
    if username:
        emit('user_offline', {'username': username}, broadcast=True)
        
        # حذف از تمام اتاق‌ها
        for room_id in list(users_in_room.keys()):
            if room_id in users_in_room:
                users_in_room[room_id] = [u for u in users_in_room[room_id] if u['username'] != username]
        
        # پایان تمام تماس‌های کاربر
        for room_id in list(active_calls.keys()):
            if username in active_calls[room_id]['participants']:
                end_call_in_room(room_id, username)

# ----------------------------
# اجرای برنامه
# ----------------------------
if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)