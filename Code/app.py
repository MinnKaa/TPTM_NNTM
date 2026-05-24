import requests
from flask import Flask, render_template_string, jsonify, request
from flask_socketio import SocketIO
import serial
import threading
import time
import sqlite3
from datetime import datetime

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# --- CẤU HÌNH ---
SERIAL_PORT = 'COM6' 
BAUD_RATE = 9600
TELEGRAM_TOKEN = '8267481843:AAErGdFSJN7-1IaUklF5hIWsM01JtvmnUgA'
TELEGRAM_CHAT_ID = '5674540480'

METER_ID = "KH-0092" 

data_store = {"flow": 0.0, "total": 0.0}
leak_timer = None 

# Biến lưu trạng thái cũ để chống spam dữ liệu trùng lên biểu đồ
last_emitted_data = {"flow": -1.0, "total": -1.0}

# --- KHỔI TẠO DATABASE ---
def init_db():
    conn = sqlite3.connect('smart_water.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS water_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meter_id TEXT,
            time TEXT,
            flow_rate REAL,
            total_liters REAL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            meter_id TEXT,
            alert_type TEXT,
            details TEXT
        )
    ''')
    # BẢNG MỚI: Quản lý thông tin hộ dân lưu trong máy
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS households (
            meter_id TEXT PRIMARY KEY,
            location TEXT
        )
    ''')
    
    # Khởi tạo dữ liệu gốc ban đầu nếu bảng trống
    cursor.execute("SELECT COUNT(*) FROM households")
    if cursor.fetchone()[0] == 0:
        cursor.executemany("INSERT INTO households (meter_id, location) VALUES (?, ?)", [
            ("KH-0092", "Tòa Nhà A - Căn 402"),
            ("KH-0093", "Tòa Nhà A - Căn 403")
        ])
    conn.commit()
    conn.close()

# --- BACKEND: LUỒNG LƯU DATABASE ĐỊNH KỲ (30 GIÂY/LẦN) ---
def save_to_db_loop():
    while True:
        time.sleep(30) 
        try:
            conn = sqlite3.connect('smart_water.db')
            cursor = conn.cursor()
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            cursor.execute("""
                INSERT INTO water_history (meter_id, time, flow_rate, total_liters) 
                VALUES (?, ?, ?, ?)
            """, (METER_ID, current_time, data_store["flow"], data_store["total"] / 1000))
            
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Lỗi ghi nhận lịch sử DB: {e}")

# --- HÀM GỬI TELEGRAM & LƯU LẠI CẢNH BÁO VÀO DB ---
def log_and_send_alert(alert_type, message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=5)
    except: pass

    try:
        conn = sqlite3.connect('smart_water.db')
        cursor = conn.cursor()
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO alerts (time, meter_id, alert_type, details) 
            VALUES (?, ?, ?, ?)
        """, (current_time, METER_ID, alert_type, message))
        conn.commit()
        conn.close()
        socketio.emit('new_alert', {"time": current_time, "type": alert_type, "msg": message})
    except Exception as e:
        print(f"Lỗi lưu cảnh báo: {e}")

# --- LOGIC KIỂM TRA BẤT THƯỜNG ---
def check_anomaly(flow):
    global leak_timer
    if flow > 3.0:
        log_and_send_alert("🚨 VỠ ỐNG", f"Cảnh báo nguy hiểm: Phát hiện dòng chảy cực đại ({flow} L/min) tại hộ {METER_ID}!")
        return
    if 0.1 <= flow <= 1.0:
        if leak_timer is None:
            leak_timer = time.time()
        elif time.time() - leak_timer > 10:
            log_and_send_alert("⚠️ RÒ RỈ NƯỚC", f"Phát hiện rò rỉ kéo dài ({flow} L/min) tại hộ {METER_ID}.")
            leak_timer = time.time() + 60 
    else:
        leak_timer = None

# --- LUỒNG ĐỌC ARDUINO (BẢN GIẢI PHÓNG LUỒNG - KHÔNG CHẶN DATA) ---
def read_arduino():
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        print(f"--- ĐANG KẾT NỐI HỆ THỐNG CỔNG {SERIAL_PORT} ---")
        ser.reset_input_buffer()
        
        while True:
            if ser.in_waiting > 0:
                try:
                    line = ser.readline().decode('utf-8', errors='ignore').strip()
                    if "|" in line:
                        parts = line.split("|")
                        if len(parts) == 2:
                            flow = float(parts[0])
                            total = float(parts[1])
                            
                            data_store["flow"] = flow
                            data_store["total"] = total
                            
                            socketio.emit('update_data', data_store)
                            check_anomaly(flow)
                except Exception as e:
                    print(f"Lỗi phân tích cú pháp: {e}")
            time.sleep(0.1)
    except Exception as e:
        print(f"Lỗi cổng Serial: {e}")

# --- GIAO DIỆN GỐC (ĐÃ CẬP NHẬT CHỌN NGÀY) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hệ thống Quản lý và Giám sát Nước Thông minh IoT</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root { --primary: #00f2fe; --secondary: #4facfe; --dark-bg: #0b0f19; --card-bg: rgba(22, 28, 45, 0.6); --text: #f8fafc; --alert: #ef4444; }
        body { font-family: 'Segoe UI', Tahoma, sans-serif; background-color: var(--dark-bg); background-image: radial-gradient(at 0% 0%, rgba(31, 38, 103, 0.3) 0, transparent 50%), radial-gradient(at 100% 100%, rgba(0, 242, 254, 0.05) 0, transparent 50%); color: var(--text); margin: 0; padding: 0; display: flex; }
        
        .sidebar { width: 260px; background: #111827; height: 100vh; position: fixed; box-shadow: 2px 0 10px rgba(0,0,0,0.5); display: flex; flex-direction: column; align-items: center; padding-top: 20px; }
        .sidebar h2 { color: var(--primary); font-size: 1.3rem; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 40px; text-align:center; padding: 0 10px;}
        .menu-btn { width: 85%; padding: 14px; margin: 8px 0; background: transparent; border: 1px solid rgba(255,255,255,0.05); color: #94a3b8; border-radius: 12px; cursor: pointer; text-align: left; font-size: 1rem; font-weight: 600; transition: all 0.3s; }
        .menu-btn:hover, .menu-btn.active { background: linear-gradient(135deg, var(--secondary), var(--primary)); color: #0b0f19; box-shadow: 0 4px 15px rgba(0, 242, 254, 0.3); transform: translateX(5px); }
        .badge-role { font-size: 0.75rem; padding: 4px 8px; border-radius: 20px; font-weight: bold; margin-top: -30px; margin-bottom: 20px;}
        .role-user { background: rgba(16, 185, 129, 0.2); color: #10b981; }
        .role-admin { background: rgba(239, 68, 68, 0.2); color: #ef4444; }

        .main-content { margin-left: 260px; padding: 40px; width: calc(100% - 260px); min-height: 100vh; box-sizing: border-box; }
        .page { display: none; }
        .page.active { display: block; animation: fadeIn 0.5s ease-in-out; }
        
        .dashboard-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 25px; margin-bottom: 30px; }
        .stat-card { background: var(--card-bg); backdrop-filter: blur(12px); border: 1px solid rgba(255,255,255,0.05); border-radius: 24px; padding: 30px; text-align: center; position: relative; overflow: hidden; transition: 0.3s; }
        .stat-card:hover { border-color: var(--primary); transform: translateY(-5px); }
        .stat-card::before { content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 4px; background: linear-gradient(to right, var(--secondary), var(--primary)); }
        .stat-title { color: #64748b; text-transform: uppercase; font-size: 0.85rem; font-weight: 700; letter-spacing: 1px; }
        .stat-value { font-size: 3.2rem; font-weight: 800; margin: 15px 0; background: linear-gradient(to right, #fff, #94a3b8); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        
        .chart-box { background: var(--card-bg); border: 1px solid rgba(255,255,255,0.05); border-radius: 24px; padding: 25px; height: 350px; box-sizing: border-box; }
        
        .table-container { background: var(--card-bg); border-radius: 20px; border: 1px solid rgba(255,255,255,0.05); overflow: hidden; margin-top: 20px; }
        table { width: 100%; border-collapse: collapse; text-align: left; }
        th, td { padding: 15px 20px; border-bottom: 1px solid rgba(255,255,255,0.05); }
        th { background: rgba(15, 23, 42, 0.8); color: var(--primary); font-size: 0.9rem; text-transform: uppercase; }
        tr:hover { background: rgba(255,255,255,0.02); }
        
        .status-pill { padding: 5px 12px; border-radius: 30px; font-size: 0.8rem; font-weight: bold; }
        .status-pill.danger { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
        .status-pill.warning { background: rgba(245, 158, 11, 0.15); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
        .status-pill.normal { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }

        /* Custom cho ô chọn ngày đồng bộ với Dark Mode */
        .filter-select { background: #1e293b; color: #fff; border: 1px solid rgba(255,255,255,0.1); padding: 8px 16px; border-radius: 8px; font-size: 0.9rem; outline: none; cursor: pointer; font-family: inherit;}
        .filter-select::-webkit-calendar-picker-indicator { filter: invert(1); cursor: pointer; } /* Đảo icon lịch sang màu trắng */
        
        .crud-btn { border: none; padding: 6px 12px; border-radius: 6px; font-weight: bold; cursor: pointer; margin-right: 5px; font-size: 0.8rem; transition: 0.2s; }
        .crud-btn:hover { opacity: 0.8; }
        .btn-add { background: linear-gradient(135deg, var(--secondary), var(--primary)); color: #0b0f19; padding: 10px 20px; border-radius: 8px; font-size: 0.9rem; }
        .btn-edit { background: #eab308; color: #0f172a; }
        .btn-delete { background: #ef4444; color: white; }
        
        .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); backdrop-filter: blur(5px); justify-content: center; align-items: center; z-index: 999; }
        .modal-content { background: #111827; border: 1px solid var(--primary); padding: 30px; border-radius: 16px; width: 400px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
        .modal-content h3 { margin-top: 0; color: var(--primary); font-size: 1.2rem; }
        .form-group { margin-bottom: 15px; display: flex; flex-direction: column; }
        .form-group label { font-size: 0.85rem; color: #94a3b8; margin-bottom: 5px; }
        .form-group input { background: #1e293b; border: 1px solid rgba(255,255,255,0.1); padding: 10px; color: white; border-radius: 8px; outline: none; font-size: 0.95rem; }
        .modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }

        @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
    </style>
</head>
<body>

    <div class="sidebar">
        <h2>Water IoT Hub</h2>
        <div class="badge-role role-user">GIAO DIỆN NGƯỜI DÙNG</div>
        <button class="menu-btn active" onclick="switchPage('user-dashboard', this)">📊 Giám Sát Trực Tiếp</button>
        <button class="menu-btn" onclick="switchPage('user-history', this)">📅 Lịch Sử Tiêu Thụ</button>
        
        <div class="badge-role role-admin" style="margin-top: 30px;">HỆ THỐNG QUẢN LÝ</div>
        <button class="menu-btn" onclick="switchPage('admin-all-meters', this)">🏢 Danh Sách Hộ Dân</button>
        <button class="menu-btn" onclick="switchPage('admin-alerts', this)">⚠️ Trung Tâm Cảnh Báo</button>
    </div>

    <div class="main-content">
        <div id="user-dashboard" class="page active">
            <h1 style="margin-top:0;">Giám Sát Lưu Lượng Nước Hộ Gia Đình</h1>
            <p style="color: #64748b; margin-bottom: 30px;">Mã số đồng hồ: <strong style="color: var(--primary); font-size:1.1rem;">{{ meter_id }}</strong></p>
            
            <div class="dashboard-grid">
                <div class="stat-card">
                    <div class="stat-title">Lưu Lượng Hiện Tại</div>
                    <div class="stat-value" id="flow" style="color: var(--primary);">0.0</div>
                    <span style="color: #64748b;">Lít / Phút</span>
                </div>
                <div class="stat-card">
                    <div class="stat-title">Tổng Tiêu Thụ Toàn Bộ</div>
                    <div class="stat-value" id="total" style="color: #10b981;">0.00</div>
                    <span style="color: #64748b;">Lít</span>
                </div>
            </div>
            
            <div class="chart-box">
                <canvas id="waterChart"></canvas>
            </div>
        </div>

        <div id="user-history" class="page">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                <h1 style="margin:0;">Nhật Ký Ghi Nhận Tiêu Thụ</h1>
                <div>
                    <label style="color:#94a3b8; font-size:0.9rem; margin-right:10px;">Chọn ngày xem:</label>
                    <input type="date" id="time-filter" class="filter-select" onchange="loadUserHistory()">
                </div>
            </div>
            <p style="color: #64748b;">Dữ liệu backend được tự động đóng băng và lưu trữ định kỳ vào database.</p>
            <div class="table-container">
                <table>
                    <thead>
                        <tr><th>Thời Gian Lưu</th><th>Mã Số Công Tơ</th><th>Lưu Lượng (L/Min)</th><th>Chỉ Số Tổng (Lít)</th></tr>
                    </thead>
                    <tbody id="user-history-table"></tbody>
                </table>
            </div>
        </div>

        <div id="admin-all-meters" class="page">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                <h1 style="margin:0;">Quản Lý Danh Sách Các Hộ Dân </h1>
                <button class="crud-btn btn-add" onclick="openModal('add')">+ Thêm Hộ Dân Mới</button>
            </div>
            <p style="color: #64748b;">Tổng quan trạng thái vận hành và thông tin chi tiết của các thiết bị đầu cuối.</p>
            <div class="table-container">
                <table>
                    <thead>
                        <tr><th>Mã Đồng Hồ</th><th>Khu Vực Địa Lý</th><th>Lưu Lượng Đang Chạy</th><th>Trạng Thái</th><th>Hành Động</th></tr>
                    </thead>
                    <tbody id="admin-meters-table"></tbody>
                </table>
            </div>
        </div>

        <div id="admin-alerts" class="page">
            <h1 style="color: var(--alert);">Trung Tâm Giám Sát Cảnh Báo Sự Cố</h1>
            <p style="color: #64748b;">Nơi tiếp nhận các cảnh báo rò rỉ liên tục hoặc nứt vỡ ống nước thời gian thực.</p>
            <div class="table-container">
                <table>
                    <thead>
                        <tr><th>Thời Gian Phát Hiện</th><th>Mã Thiết Bị</th><th>Mức Độ Loại Cảnh Báo</th><th>Nội Dung Chi Tiết Sự Cố</th></tr>
                    </thead>
                    <tbody id="admin-alerts-table"></tbody>
                </table>
            </div>
        </div>
    </div>

    <div id="house-modal" class="modal">
        <div class="modal-content">
            <h3 id="modal-title">Thêm Hộ Dân</h3>
            <form id="house-form" onsubmit="saveHousehold(event)">
                <input type="hidden" id="action-type">
                <div class="form-group">
                    <label>Mã số đồng hồ</label>
                    <input type="text" id="modal-meter-id" placeholder="VD: KH-0094" required>
                </div>
                <div class="form-group">
                    <label>Khu vực địa lý / Căn hộ</label>
                    <input type="text" id="modal-location" placeholder="VD: Tòa Nhà B - Căn 101" required>
                </div>
                <div class="modal-actions">
                    <button type="button" class="crud-btn" style="background:#475569; color:white;" onclick="closeModal()">Hủy</button>
                    <button type="submit" class="crud-btn" style="background:var(--primary); color:#0b0f19;">Xác nhận</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        const socket = io();
        let currentFlowRealtime = 0.0; 
        
        // ĐÃ SỬA: Tự động điền ngày hôm nay vào ô input khi load trang xong
        document.addEventListener("DOMContentLoaded", function() {
            const dateInput = document.getElementById('time-filter');
            const today = new Date();
            const yyyy = today.getFullYear();
            const mm = String(today.getMonth() + 1).padStart(2, '0');
            const dd = String(today.getDate()).padStart(2, '0');
            dateInput.value = `${yyyy}-${mm}-${dd}`;
        });

        function switchPage(pageId, btn) {
            document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.menu-btn').forEach(b => b.classList.remove('active'));
            
            document.getElementById(pageId).classList.add('active');
            btn.classList.add('active');
            
            if(pageId === 'user-history') loadUserHistory();
            if(pageId === 'admin-all-meters') loadAdminMeters();
            if(pageId === 'admin-alerts') loadAdminAlerts();
        }

        const ctx = document.getElementById('waterChart').getContext('2d');
        let chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [{
                    label: 'Dòng chảy thực tế (L/min)',
                    data: [],
                    borderColor: '#00f2fe',
                    backgroundColor: 'rgba(0, 242, 254, 0.05)',
                    borderWidth: 3,
                    fill: true,
                    tension: 0.35,
                    pointRadius: 2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    y: { beginAtZero: true, grid: { color: 'rgba(255,255,255,0.03)' }, ticks: { color: '#94a3b8' } },
                    x: { grid: { display: false }, ticks: { color: '#94a3b8' } }
                },
                plugins: { legend: { display: false } }
            }
        });

        socket.on('update_data', function(data) {
            currentFlowRealtime = data.flow;
            document.getElementById('flow').innerText = data.flow;
            document.getElementById('total').innerText = (data.total / 1000).toFixed(2);
            
            const adminInlineFlow = document.getElementById('flow-KH-0092');
            const adminInlinePill = document.getElementById('pill-KH-0092');
            if (adminInlineFlow) {
                adminInlineFlow.innerText = data.flow + " L/min";
            }
            if (adminInlinePill) {
                if(data.flow > 3.0) {
                    adminInlinePill.className = "status-pill danger"; adminInlinePill.innerText = "🚨 VỠ ỐNG";
                } else if (data.flow >= 0.1 && data.flow <= 1.0) {
                    adminInlinePill.className = "status-pill warning"; adminInlinePill.innerText = "⚠️ Đang rò rỉ";
                } else {
                    adminInlinePill.className = "status-pill normal"; adminInlinePill.innerText = "Đang kết nối";
                }
            }

            const now = new Date().toLocaleTimeString();
            chart.data.labels.push(now);
            chart.data.datasets[0].data.push(data.flow);
            if (chart.data.labels.length > 12) {
                chart.data.labels.shift();
                chart.data.datasets[0].data.shift();
            }
            chart.update();
        });

        socket.on('new_alert', function(alert) {
            const tbody = document.getElementById('admin-alerts-table');
            if (tbody) {
                const badge = alert.type.includes('VỠ') ? 'danger' : 'warning';
                const row = `<tr><td>${alert.time}</td><td>KH-0092</td><td><span class="status-pill ${badge}">${alert.type}</span></td><td>${alert.msg}</td></tr>`;
                tbody.innerHTML = row + tbody.innerHTML;
            }
        });

        // --- ĐÃ SỬA: LẤY GIÁ TRỊ NGÀY GỬI LÊN BACKEND ---
        function loadUserHistory() {
            const selectedDate = document.getElementById('time-filter').value;
            fetch(`/api/history?date=${selectedDate}`)
                .then(res => res.json())
                .then(data => {
                    let html = '';
                    data.forEach(r => {
                        html += `<tr><td>${r[2]}</td><td>${r[1]}</td><td>${r[3]}</td><td>${r[4].toFixed(2)}</td></tr>`;
                    });
                    document.getElementById('user-history-table').innerHTML = html || "<tr><td colspan='4' style='text-align:center;color:#64748b;'>Không có dữ liệu trong ngày đã chọn</td></tr>";
                });
        }

        function loadAdminMeters() {
            fetch('/api/households')
                .then(res => res.json())
                .then(data => {
                    let html = '';
                    data.forEach(r => {
                        const isMainNode = (r[0] === 'KH-0092');
                        let displayFlow = "0.0 L/min";
                        let statusPill = '<span class="status-pill normal" style="opacity: 0.5;">Ngoại tuyến</span>';
                        
                        if(isMainNode) {
                            displayFlow = currentFlowRealtime + " L/min";
                            if(currentFlowRealtime > 3.0) statusPill = '<span class="status-pill danger" id="pill-KH-0092">🚨 VỠ ỐNG</span>';
                            else if (currentFlowRealtime >= 0.1 && currentFlowRealtime <= 1.0) statusPill = '<span class="status-pill warning" id="pill-KH-0092">⚠️ Đang rò rỉ</span>';
                            else statusPill = '<span class="status-pill normal" id="pill-KH-0092">Đang kết nối</span>';
                        }
                        
                        html += `<tr>
                            <td><strong>${r[0]}</strong> ${isMainNode ? '(Thiết bị)' : ''}</td>
                            <td>${r[1]}</td>
                            <td id="flow-${r[0]}">${displayFlow}</td>
                            <td>${statusPill}</td>
                            <td>
                                <button class="crud-btn btn-edit" onclick="openModal('edit', '${r[0]}', '${r[1]}')">Sửa</button>
                                <button class="crud-btn btn-delete" onclick="deleteHousehold('${r[0]}')">Xóa</button>
                            </td>
                        </tr>`;
                    });
                    document.getElementById('admin-meters-table').innerHTML = html;
                });
        }

        function openModal(type, id='', loc='') {
            document.getElementById('house-modal').style.display = 'flex';
            document.getElementById('action-type').value = type;
            if(type === 'add') {
                document.getElementById('modal-title').innerText = "Thêm Hộ Dân Mới Mạng Lưới";
                document.getElementById('modal-meter-id').value = "";
                document.getElementById('modal-meter-id').disabled = false;
                document.getElementById('modal-location').value = "";
            } else {
                document.getElementById('modal-title').innerText = "Chỉnh Sửa Thông Tin Căn Hộ";
                document.getElementById('modal-meter-id').value = id;
                document.getElementById('modal-meter-id').disabled = true; 
                document.getElementById('modal-location').value = loc;
            }
        }
        function closeModal() { document.getElementById('house-modal').style.display = 'none'; }

        function saveHousehold(e) {
            e.preventDefault();
            const type = document.getElementById('action-type').value;
            const meter_id = document.getElementById('modal-meter-id').value;
            const location = document.getElementById('modal-location').value;
            
            fetch('/api/households', {
                method: type === 'add' ? 'POST' : 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ meter_id, location })
            }).then(() => {
                closeModal();
                loadAdminMeters();
            });
        }

        function deleteHousehold(meter_id) {
            if(confirm(`Bạn chắc chắn có muốn xóa mã đồng hồ ${meter_id} khỏi cơ sở dữ liệu?`)) {
                fetch(`/api/households?meter_id=${meter_id}`, { method: 'DELETE' })
                    .then(() => loadAdminMeters());
            }
        }

        function loadAdminAlerts() {
            fetch('/api/alerts')
                .then(res => res.json())
                .then(data => {
                    let html = '';
                    data.forEach(r => {
                        const badge = r[3].includes('VỠ') ? 'danger' : 'warning';
                        html += `<tr><td>${r[1]}</td><td>${r[2]}</td><td><span class="status-pill ${badge}">${r[3]}</span></td><td>${r[4]}</td></tr>`;
                    });
                    document.getElementById('admin-alerts-table').innerHTML = html || "<tr><td colspan='4'>An toàn - Không phát hiện sự cố rò rỉ nào!</td></tr>";
                });
        }
    </script>
</body>
</html>
"""

# --- API ENDPOINTS ---
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, meter_id=METER_ID)

# --- ĐÃ SỬA: API LỊCH SỬ NHẬN THAM SỐ DATE ĐỂ TRUY VẤN THEO NGÀY CHỌN ---
@app.route('/api/history')
def get_history():
    selected_date = request.args.get('date')
    
    conn = sqlite3.connect('smart_water.db')
    cursor = conn.cursor()
    
    if selected_date:
        # Lọc chính xác theo ngày người dùng chọn (Chuỗi ngày dạng YYYY-MM-DD)
        cursor.execute("SELECT * FROM water_history WHERE time LIKE ? ORDER BY id DESC LIMIT 100", (f"{selected_date}%",))
    else:
        # Nếu không truyền ngày, mặc định trả về của ngày hôm nay
        today_str = datetime.now().strftime("%Y-%m-%d")
        cursor.execute("SELECT * FROM water_history WHERE time LIKE ? ORDER BY id DESC LIMIT 100", (f"{today_str}%",))
        
    rows = cursor.fetchall()
    conn.close()
    return jsonify(rows)

# API CRUD TOÀN DIỆN DANH SÁCH CÁC HỘ DÂN LƯU TRONG DB
@app.route('/api/households', methods=['GET', 'POST', 'PUT', 'DELETE'])
def handle_households():
    conn = sqlite3.connect('smart_water.db')
    cursor = conn.cursor()
    
    if request.method == 'GET':
        cursor.execute("SELECT * FROM households ORDER BY meter_id ASC")
        rows = cursor.fetchall()
        conn.close()
        return jsonify(rows)
        
    elif request.method == 'POST':
        data = request.json
        try:
            cursor.execute("INSERT INTO households (meter_id, location) VALUES (?, ?)", (data['meter_id'], data['location']))
            conn.commit()
        except: pass
        conn.close()
        return jsonify({"status": "success"})
        
    elif request.method == 'PUT':
        data = request.json
        cursor.execute("UPDATE households SET location = ? WHERE meter_id = ?", (data['location'], data['meter_id']))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
        
    elif request.method == 'DELETE':
        meter_id = request.args.get('meter_id')
        cursor.execute("DELETE FROM households WHERE meter_id = ?", (meter_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})

@app.route('/api/alerts')
def get_alerts():
    conn = sqlite3.connect('smart_water.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM alerts ORDER BY id DESC LIMIT 50')
    rows = cursor.fetchall()
    conn.close()
    return jsonify(rows)

if __name__ == '__main__':
    init_db()
    threading.Thread(target=read_arduino, daemon=True).start()
    threading.Thread(target=save_to_db_loop, daemon=True).start()
    socketio.run(app, debug=False, port=5000, use_reloader=False)