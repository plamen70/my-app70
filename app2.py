import streamlit as st
import sqlite3
import pandas as pd
from datetime import datetime, date, timedelta
import io
import os
import hashlib
import json
import urllib.request

# --- ИМПОРТИРАНЕ НА REPORTLAB ЗА PDF ---
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

# --- 1. НАСТРОЙКА НА СТРАНИЦАТА ---
st.set_page_config(
    page_title="Управление на склад (Мулти-фирмен)",
    page_icon="🏢",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
    <style>
    .main { background-color: #f8f9fa; }
    .stDownloadButton > button {
        background-color: #0d6efd !important;
        color: white !important;
        font-weight: bold;
        border-radius: 8px;
        border: none;
        padding: 10px 20px;
    }
    .stDownloadButton > button:hover { background-color: #0b5ed7 !important; }
    </style>
""", unsafe_allow_html=True)


# --- 2. БАЗА ДАННИ И МИГРАЦИЯ ---
@st.cache_resource
def get_db_connection():
    conn = sqlite3.connect('inventory_v2.db', check_same_thread=False, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


conn = get_db_connection()


def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


def init_db():
    cursor = conn.cursor()

    # Таблица за фирми
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            eik TEXT DEFAULT '',
            address TEXT DEFAULT '',
            mol TEXT DEFAULT ''
        )
    ''')

    cursor.execute("PRAGMA table_info(companies)")
    cols = [c[1] for c in cursor.fetchall()]
    if "address" not in cols:
        cursor.execute("ALTER TABLE companies ADD COLUMN address TEXT DEFAULT ''")
    if "mol" not in cols:
        cursor.execute("ALTER TABLE companies ADD COLUMN mol TEXT DEFAULT ''")

    # Таблица за потребители
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            role TEXT DEFAULT 'admin'
        )
    ''')

    # Таблица за артикули
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            category TEXT,
            quantity INTEGER NOT NULL DEFAULT 0,
            min_limit INTEGER DEFAULT 5,
            price REAL DEFAULT 0.0,
            FOREIGN KEY (company_id) REFERENCES companies (id)
        )
    ''')

    # Таблица за история на движенията
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS movement_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            action_type TEXT NOT NULL,
            quantity_change INTEGER NOT NULL,
            unit_price REAL DEFAULT 0.0,
            doc_type TEXT,
            doc_number TEXT,
            doc_date TEXT,
            supplier_name TEXT DEFAULT '',
            supplier_eik TEXT DEFAULT '',
            supplier_address TEXT DEFAULT '',
            timestamp TEXT NOT NULL,
            FOREIGN KEY (company_id) REFERENCES companies (id)
        )
    ''')

    cursor.execute("PRAGMA table_info(movement_history)")
    move_cols = [c[1] for c in cursor.fetchall()]
    if "supplier_name" not in move_cols:
        cursor.execute("ALTER TABLE movement_history ADD COLUMN supplier_name TEXT DEFAULT ''")
    if "supplier_eik" not in move_cols:
        cursor.execute("ALTER TABLE movement_history ADD COLUMN supplier_eik TEXT DEFAULT ''")
    if "supplier_address" not in move_cols:
        cursor.execute("ALTER TABLE movement_history ADD COLUMN supplier_address TEXT DEFAULT ''")

    cursor.execute("SELECT COUNT(*) FROM companies")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO companies (name, eik, address, mol) VALUES ('Основна Фирма ООД', '123456789', 'гр. София, ул. Централна 1', 'Иван Иванов')")

    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", ("admin", hash_password("admin")))

    conn.commit()


init_db()


# --- 3. СПРАВКА В ТЪРГОВСКИЯ РЕГИСТЪР ---
def fetch_company_info_by_eik(eik_number):
    eik_clean = eik_number.strip()
    if not eik_clean or len(eik_clean) not in [9, 13]:
        return None, "❌ Невалиден ЕИК/БУЛСТАТ! Номерът трябва да е точно 9 или 13 цифри."

    try:
        url = f"https://portal.registryagency.bg/CR/api/Deeds/{eik_clean}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Referer': 'https://portal.registryagency.bg/'
        }
        req = urllib.request.Request(url, headers=headers)

        with urllib.request.urlopen(req, timeout=6) as response:
            if response.status == 200:
                data = json.loads(response.read().decode('utf-8'))

                company_name = data.get('companyName', '').strip()
                legal_form = data.get('legalForm', '').strip()
                full_name = f"{company_name} {legal_form}".strip()

                seat_info = data.get('seat', {})
                address = seat_info.get('address', '') if isinstance(seat_info, dict) else ''

                representatives = data.get('representatives', [])
                mol_name = ""
                if representatives and isinstance(representatives, list):
                    mol_name = representatives[0].get('name', '')

                if full_name:
                    return {
                        'name': full_name,
                        'address': address if address else "",
                        'mol': mol_name if mol_name else ""
                    }, None

    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, f"⚠️ Фирма с ЕИК {eik_clean} не бе намерена в Търговския регистър."
    except Exception:
        pass

    return None, "⚠️ Публичният сървър на Агенция по вписванията не върна данни в момента. Въведете данните ръчно."


# --- 4. СЕСИЯ И АВТЕНТИКАЦИЯ ---
if 'logged_in' not in st.session_state:
    st.session_state['logged_in'] = False
if 'username' not in st.session_state:
    st.session_state['username'] = ""
if 'current_company_id' not in st.session_state:
    st.session_state['current_company_id'] = 1

# --- 5. ЕКРАН ЗА ВХОД (LOGIN) ---
if not st.session_state['logged_in']:
    st.markdown("<h2 style='text-align: center;'>🔒 Вход в Складовата Система</h2>", unsafe_allow_html=True)

    with st.form("login_form"):
        col1, col2, col3 = st.columns([1, 2, 1])
        with col2:
            user_input = st.text_input("Потребителско име")
            pass_input = st.text_input("Парола", type="password")
            submit_login = st.form_submit_button("🔓 Вход")

            if submit_login:
                cursor = conn.cursor()
                cursor.execute("SELECT password FROM users WHERE username = ?", (user_input.strip(),))
                res = cursor.fetchone()

                if res and res[0] == hash_password(pass_input):
                    st.session_state['logged_in'] = True
                    st.session_state['username'] = user_input.strip()
                    st.success("Успешен вход!")
                    st.rerun()
                else:
                    st.error("❌ Невалидно потребителско име или парола!")

    st.info("💡 **Начални данни за вход:** Потребител: `admin` | Парола: `admin`")
    st.stop()

# --- 6. СТРАНИЧЕН ПАНЕЛ ---
cursor = conn.cursor()
cursor.execute("SELECT id, name FROM companies")
companies = cursor.fetchall()
company_dict = {comp[1]: comp[0] for comp in companies}

st.sidebar.markdown(f"👤 **Потребител:** `{st.session_state['username']}`")

selected_company_name = st.sidebar.selectbox(
    "🏢 Изберете фирма/клиент:",
    list(company_dict.keys())
)
st.session_state['current_company_id'] = company_dict[selected_company_name]
current_company_id = st.session_state['current_company_id']

if st.sidebar.button("🚪 Изход от системата"):
    st.session_state['logged_in'] = False
    st.session_state['username'] = ""
    st.rerun()

st.sidebar.markdown("---")

menu = [
    "📊 Табло & Наличности",
    "📝 Вход / Изход с Документ",
    "➕ Добавяне на Нов Артикул",
    "✏️ Редакция / Изтриване",
    "📜 История и Документи",
    "⚠️ Критични Наличности",
    "🏢 Управление на Фирми",
    "📦 Архивиране и Възстановяване",
    "👤 Профил & Пароли"
]
choice = st.sidebar.radio("Навигация", menu)


# --- 7. ГЕНЕРИРАНЕ НА PDF С КИРИЛИЦА ---
def generate_pdf(df, title="Складова Справка"):
    buffer = io.BytesIO()
    font_name = "Helvetica"
    font_name_bold = "Helvetica-Bold"
    try:
        font_path = "C:\\Windows\\Fonts\\arial.ttf"
        font_path_bold = "C:\\Windows\\Fonts\\arialbd.ttf"
        if os.path.exists(font_path):
            pdfmetrics.registerFont(TTFont('ArialCustom', font_path))
            pdfmetrics.registerFont(TTFont('ArialCustom-Bold', font_path_bold))
            font_name = 'ArialCustom'
            font_name_bold = 'ArialCustom-Bold'
    except Exception:
        pass

    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    elements = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontName=font_name_bold, fontSize=16,
                                 leading=20, alignment=1, spaceAfter=15)
    normal_style = ParagraphStyle('NormalStyle', parent=styles['Normal'], fontName=font_name, fontSize=8)

    elements.append(Paragraph(f"<b>{title}</b>", title_style))
    elements.append(
        Paragraph(f"<b>Фирма:</b> {selected_company_name} | <b>Дата:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                  normal_style))
    elements.append(Spacer(1, 15))

    table_data = []
    headers = [Paragraph(f"<b>{str(col)}</b>",
                         ParagraphStyle('HeaderStyle', parent=normal_style, fontName=font_name_bold,
                                        textColor=colors.whitesmoke, alignment=1)) for col in df.columns]
    table_data.append(headers)

    for idx, row in df.iterrows():
        row_cells = [Paragraph(str(val if val is not None else ''), normal_style) for val in row]
        table_data.append(row_cells)

    table = Table(table_data)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#0d6efd")),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
    ]))

    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    return buffer


# --- 8. ОСНОВНИ МОДУЛИ ---

# --- 8.1. ТАБЛО & НАЛИЧНОСТИ ---
if choice == "📊 Табло & Наличности":
    st.subheader(f"📊 Склад на: {selected_company_name}")

    df = pd.read_sql_query(
        "SELECT id AS ID, name AS 'Артикул', category AS 'Категория', quantity AS 'Количество', min_limit AS 'Мин. праг', price AS 'Ед. цена (лв.)' FROM inventory WHERE company_id = ?",
        conn, params=(current_company_id,)
    )

    if not df.empty:
        df["Обща стойност (лв.)"] = df["Количество"] * df["Ед. цена (лв.)"]

        kpi1, kpi2, kpi3, kpi4 = st.columns(4)
        kpi1.metric("📦 Номенклатури", f"{len(df)} бр.")
        kpi2.metric("🔢 Общо количества", f"{df['Количество'].sum():,} бр.".replace(",", " "))
        kpi3.metric("💰 Обща стойност", f"{df['Обща стойност (лв.)'].sum():,.2f} лв.".replace(",", " "))

        critical_count = len(df[df['Количество'] <= df['Мин. праг']])
        kpi4.metric("⚠️ Критични позиции", f"{critical_count} бр.", delta_color="inverse")

        st.markdown("---")
        st.markdown("### 📋 Списък на складовите наличности")
        st.dataframe(df, use_container_width=True, height=300)

        col_pdf, col_csv = st.columns(2)
        with col_pdf:
            if PDF_AVAILABLE:
                pdf_data = generate_pdf(df, f"Справка Наличности - {selected_company_name}")
                st.download_button("📥 Изтегли Наличностите в PDF", data=pdf_data,
                                   file_name=f"nalichnosti_{datetime.now().strftime('%Y%m%d')}.pdf",
                                   mime="application/pdf")
        with col_csv:
            csv_data = df.to_csv(index=False).encode('utf-8-sig')
            st.download_button("📥 Изтегли Наличностите в CSV (Excel)", data=csv_data,
                               file_name=f"nalichnosti_{datetime.now().strftime('%Y%m%d')}.csv", mime="text/csv")
    else:
        st.info("Няма въведени артикули за тази фирма.")


# --- 8.2. ВХОД / ИЗХОД С ДОКУМЕНТ ---
elif choice == "📝 Вход / Изход с Документ":
    st.subheader(f"📝 Движение на стока за: {selected_company_name}")

    cursor.execute("SELECT id, name, quantity, price FROM inventory WHERE company_id = ?", (current_company_id,))
    items = cursor.fetchall()

    if items:
        item_dict = {
            f"{item[1]} (Наличност: {item[2]} бр. | Цена: {item[3]:.2f} лв.)": (item[0], item[1], item[2], item[3]) for
            item in items}

        if 'sup_name_val' not in st.session_state:
            st.session_state['sup_name_val'] = ""
        if 'sup_addr_val' not in st.session_state:
            st.session_state['sup_addr_val'] = ""

        with st.expander("🔍 Бърза справка за Контрагент по БУЛСТАТ / ЕИК"):
            c_eik, c_btn = st.columns([3, 1])
            eik_sup_search = c_eik.text_input("ЕИК на контрагента:", key="sup_eik_search")
            if c_btn.button("Търси в Търговския регистър"):
                if eik_sup_search:
                    res_sup, err_sup = fetch_company_info_by_eik(eik_sup_search)
                    if res_sup:
                        st.session_state['sup_name_val'] = res_sup['name']
                        st.session_state['sup_addr_val'] = res_sup['address']
                        st.success(f"Намерена фирма: {res_sup['name']}")
                    else:
                        st.warning(err_sup)

        action = st.radio("Изберете операция:", ["Заприходяване (Вход +)", "Изписване (Изход -)"], key="action_select")

        is_entry = "Вход" in action
        party_label = "Доставчик" if is_entry else "Клиент"

        with st.form("movement_form"):
            selected_option = st.selectbox("Изберете артикул*", list(item_dict.keys()))
            item_id, item_name, current_qty, current_price = item_dict[selected_option]

            c2, c3 = st.columns(2)
            with c2:
                change_qty = st.number_input("Количество (бр.)*", min_value=1, step=1, value=1)
            with c3:
                unit_price = st.number_input("Единична цена (лв.)", min_value=0.0, step=0.10,
                                             value=float(current_price), format="%.2f")

            st.markdown("##### 📄 Данни за Документа")
            d1, d2, d3 = st.columns(3)
            with d1:
                doc_type = st.selectbox("Вид документ", ["Фактура", "Стокова разписка", "Приемо-предавателен протокол",
                                                         "Инвентаризационен акт", "Друг"])
            with d2:
                doc_number = st.text_input("Номер на документ*", placeholder="напр. 0000001234")
            with d3:
                doc_date = st.date_input("Дата на документ", date.today())

            st.markdown(f"##### 🏢 Данни за Контрагента ({party_label})")
            sup1, sup2, sup3 = st.columns(3)
            with sup1:
                supplier_name = st.text_input(f"Фирма ({party_label})", value=st.session_state['sup_name_val'],
                                              placeholder="напр. Клиент ЕООД")
            with sup2:
                supplier_eik = st.text_input(f"ЕИК / БУЛСТАТ ({party_label})",
                                             value=eik_sup_search if 'sup_eik_search' in st.session_state and eik_sup_search else "",
                                             placeholder="напр. 123456789")
            with sup3:
                supplier_address = st.text_input(f"Адрес ({party_label})", value=st.session_state['sup_addr_val'],
                                                 placeholder="гр. Попово, ул. Промишлена 1")

            submit_move = st.form_submit_button("💾 Регистрирай движението")

            if submit_move:
                if not doc_number.strip():
                    st.error("⚠️ Въведете номер на документа!")
                else:
                    new_qty = current_qty + change_qty if is_entry else current_qty - change_qty
                    if new_qty < 0:
                        st.error("❌ Грешка: Нямате достатъчна наличност!")
                    else:
                        action_text = "Вход (+)" if is_entry else "Изход (-)"
                        change_val = change_qty if is_entry else -change_qty

                        cursor.execute("UPDATE inventory SET quantity = ?, price = ? WHERE id = ?",
                                       (new_qty, unit_price, item_id))
                        cursor.execute(
                            '''INSERT INTO movement_history (
                                company_id, item_name, action_type, quantity_change, unit_price, 
                                doc_type, doc_number, doc_date, supplier_name, supplier_eik, supplier_address, timestamp
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (current_company_id, item_name, action_text, change_val, unit_price,
                             doc_type, doc_number, doc_date.strftime("%d.%m.%Y"),
                             supplier_name.strip(), supplier_eik.strip(), supplier_address.strip(),
                             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                        )
                        conn.commit()
                        st.success(f"✅ Запазено! Ново количество за '{item_name}': {new_qty} бр.")
                        st.rerun()
    else:
        st.info("Няма налични артикули за тази фирма.")


# --- 8.3. ДОБАВЯНЕ НА НОВ АРТИКУЛ ---
elif choice == "➕ Добавяне на Нов Артикул":
    st.subheader(f"➕ Нов продукт за: {selected_company_name}")

    with st.form("add_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            name = st.text_input("Наименование на артикула*")
            category = st.text_input("Категория")
            price = st.number_input("Начална единична цена (лв.)", min_value=0.0, step=0.10, value=0.0)
        with c2:
            quantity = st.number_input("Начално количество", min_value=0, step=1, value=0)
            min_limit = st.number_input("Минимален праг", min_value=1, step=1, value=5)

        submit = st.form_submit_button("Запази артикула")

        if submit:
            if name.strip() != "":
                cursor.execute(
                    "INSERT INTO inventory (company_id, name, category, quantity, min_limit, price) VALUES (?, ?, ?, ?, ?, ?)",
                    (current_company_id, name, category, quantity, min_limit, price)
                )
                if quantity > 0:
                    cursor.execute(
                        '''INSERT INTO movement_history (
                            company_id, item_name, action_type, quantity_change, unit_price, 
                            doc_type, doc_number, doc_date, supplier_name, supplier_eik, supplier_address, timestamp
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (current_company_id, name, "Начална заприходеност", quantity, price, "Протокол", "0001",
                         date.today().strftime("%d.%m.%Y"), "", "", "", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    )
                conn.commit()
                st.success(f"🎉 Успешно добавен артикул '{name}'!")
            else:
                st.error("Наименованието е задължително!")


# --- 8.4. РЕДАКЦИЯ / ИЗТРИВАНЕ ---
elif choice == "✏️ Редакция / Изтриване":
    st.subheader(f"✏️ Редакция и премахване на артикули за: {selected_company_name}")

    cursor.execute("SELECT id, name, category, min_limit, price FROM inventory WHERE company_id = ?",
                   (current_company_id,))
    items = cursor.fetchall()

    if items:
        item_dict = {f"{item[1]} (Категория: {item[2]})": item for item in items}
        selected_item_name = st.selectbox("Изберете артикул за промяна", list(item_dict.keys()))
        selected_item = item_dict[selected_item_name]

        item_id, item_name, item_cat, item_limit, item_price = selected_item

        st.markdown("---")
        c1, c2 = st.columns(2)
        with c1:
            new_name = st.text_input("Име на артикула", value=item_name)
            new_cat = st.text_input("Категория", value=item_cat if item_cat else "")
        with c2:
            new_limit = st.number_input("Минимален праг", min_value=1, value=int(item_limit))
            new_price = st.number_input("Цена (лв.)", min_value=0.0, value=float(item_price), format="%.2f")

        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            if st.button("💾 Запази промените"):
                cursor.execute(
                    "UPDATE inventory SET name = ?, category = ?, min_limit = ?, price = ? WHERE id = ?",
                    (new_name, new_cat, new_limit, new_price, item_id)
                )
                conn.commit()
                st.success("✅ Промените са запазени!")
                st.rerun()

        with col_btn2:
            if st.button("🗑️ Изтрий артикула"):
                cursor.execute("DELETE FROM inventory WHERE id = ?", (item_id,))
                conn.commit()
                st.warning(f"Артикулът '{item_name}' бе изтрит успешно!")
                st.rerun()
    else:
        st.info("Няма налични артикули.")


# --- 8.5. ИСТОРИЯ И ДОКУМЕНТИ ---
elif choice == "📜 История и Документи":
    st.subheader(f"📜 Журнал на движенията за: {selected_company_name}")

    df_history = pd.read_sql_query(
        '''SELECT id AS 'ID', timestamp AS 'Дата/Час', doc_type AS 'Вид Документ', doc_number AS '№ Документ', 
                  doc_date AS 'Дата Документ', supplier_name AS 'Контрагент (Доставчик/Клиент)', 
                  supplier_eik AS 'ЕИК Контрагент', supplier_address AS 'Адрес Контрагент',
                  item_name AS 'Артикул', action_type AS 'Операция', quantity_change AS 'Количество', unit_price AS 'Ед. цена (лв.)'
           FROM movement_history WHERE company_id = ? ORDER BY id DESC''',
        conn, params=(current_company_id,)
    )

    if not df_history.empty:
        df_history["Обща Стойност (лв.)"] = df_history["Количество"].abs() * df_history["Ед. цена (лв.)"]
        st.dataframe(df_history, use_container_width=True)

        if PDF_AVAILABLE:
            pdf_history = generate_pdf(df_history, f"Журнал Движения - {selected_company_name}")
            st.download_button("📥 Изтегли Журнала в PDF", data=pdf_history,
                               file_name=f"zhurnal_{datetime.now().strftime('%Y%m%d')}.pdf", mime="application/pdf")
    else:
        st.info("Няма регистрирани движения.")


# --- 8.6. КРИТИЧНИ НАЛИЧНОСТИ ---
elif choice == "⚠️ Критични Наличности":
    st.subheader(f"⚠️ Критични наличности за: {selected_company_name}")

    df_crit = pd.read_sql_query(
        "SELECT id AS ID, name AS 'Артикул', category AS 'Категория', quantity AS 'Наличност', min_limit AS 'Минимален праг', price AS 'Ед. цена (лв.)' FROM inventory WHERE company_id = ? AND quantity <= min_limit",
        conn, params=(current_company_id,)
    )

    if not df_crit.empty:
        st.warning(f"Намерени са {len(df_crit)} артикула под минималния праг!")
        st.dataframe(df_crit, use_container_width=True)
    else:
        st.success("🎉 Всички артикули са над минималния праг!")


# --- 8.7. УПРАВЛЕНИЕ НА ФИРМИ ---
elif choice == "🏢 Управление на Фирми":
    st.subheader("🏢 Добавяне на нова фирма/клиент с реална проверка по ЕИК")

    col_eik, col_btn = st.columns([3, 1])
    with col_eik:
        eik_search = st.text_input("Въведете БУЛСТАТ / ЕИК номер:", placeholder="напр. 831011527")
    with col_btn:
        st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
        check_eik = st.button("🔍 Провери в Търговския регистър")

    if 'fetched_comp' not in st.session_state:
        st.session_state['fetched_comp'] = {'name': '', 'address': '', 'mol': ''}

    if check_eik and eik_search:
        fetched_info, err_msg = fetch_company_info_by_eik(eik_search)
        if fetched_info:
            st.session_state['fetched_comp'] = fetched_info
            st.success("✅ Данните за фирмата бяха намерени в Търговския регистър!")
        else:
            st.warning(err_msg)

    st.markdown("---")

    with st.form("company_form", clear_on_submit=True):
        st.markdown("#### Данни за новата фирма:")
        comp_name = st.text_input("Наименование на фирмата*", value=st.session_state['fetched_comp']['name'])
        comp_eik = st.text_input("ЕИК / БУЛСТАТ", value=eik_search if eik_search else "")
        comp_address = st.text_input("Адрес на фирмата", value=st.session_state['fetched_comp']['address'])
        comp_mol = st.text_input("Материално отговорно лице (МОЛ)", value=st.session_state['fetched_comp']['mol'])

        submit_comp = st.form_submit_button("➕ Добави фирмата в системата")

        if submit_comp:
            if comp_name.strip():
                try:
                    cursor.execute(
                        "INSERT INTO companies (name, eik, address, mol) VALUES (?, ?, ?, ?)",
                        (comp_name.strip(), comp_eik.strip(), comp_address.strip(), comp_mol.strip())
                    )
                    conn.commit()
                    st.session_state['fetched_comp'] = {'name': '', 'address': '', 'mol': ''}
                    st.success(f"🎉 Фирмата '{comp_name}' беше добавена успешно!")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Фирма с такова име вече съществува!")
            else:
                st.error("Името на фирмата е задължително!")

    st.markdown("---")
    st.markdown("### 📋 Списък на регистрираните фирми")
    df_comp = pd.read_sql_query(
        "SELECT id AS ID, name AS 'Фирма', eik AS 'ЕИК/БУЛСТАТ', address AS 'Адрес', mol AS 'МОЛ' FROM companies", conn)
    st.dataframe(df_comp, use_container_width=True)


# --- 8.8. АРХИВИРАНЕ И ВЪЗСТАНОВЯВАНЕ ПО ПЕРИОД ИЛИ ДАТИ ---
elif choice == "📦 Архивиране и Възстановяване":
    st.subheader("📦 Архивиране и възстановяване на базата данни")

    col_bak1, col_bak2 = st.columns(2)

    with col_bak1:
        st.markdown("#### 📥 Създаване на архив (Backup)")

        period_option = st.selectbox(
            "Изберете период за филтриране на движенията:",
            ["За 1 месец", "За 6 месеца", "За 1 година", "От дата до дата (произволен)", "Всички данни (Пълен архив)"]
        )

        today_date = date.today()
        start_date = today_date
        end_date = today_date

        if period_option == "За 1 месец":
            start_date = today_date - timedelta(days=30)
        elif period_option == "За 6 месеца":
            start_date = today_date - timedelta(days=180)
        elif period_option == "За 1 година":
            start_date = today_date - timedelta(days=365)
        elif period_option == "От дата до дата (произволен)":
            c_d1, c_d2 = st.columns(2)
            with c_d1:
                start_date = st.date_input("От дата:", value=today_date - timedelta(days=30))
            with c_d2:
                end_date = st.date_input("До дата:", value=today_date)

        if st.button("💾 Генерирай Архив за изтегляне"):
            df_companies = pd.read_sql_query("SELECT * FROM companies", conn)
            df_inventory = pd.read_sql_query("SELECT * FROM inventory", conn)

            if period_option == "Всички данни (Пълен архив)":
                df_movements = pd.read_sql_query("SELECT * FROM movement_history", conn)
                period_label = "Пълен архив"
            else:
                start_str = start_date.strftime("%Y-%m-%d 00:00:00")
                end_str = (end_date + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
                df_movements = pd.read_sql_query(
                    "SELECT * FROM movement_history WHERE timestamp >= ? AND timestamp < ?",
                    conn, params=(start_str, end_str)
                )
                period_label = f"{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}"

            backup_data = {
                'created_at': datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                'period': period_label,
                'companies': df_companies.to_dict(orient='records'),
                'inventory': df_inventory.to_dict(orient='records'),
                'movement_history': df_movements.to_dict(orient='records')
            }

            json_bytes = json.dumps(backup_data, ensure_ascii=False, indent=2).encode('utf-8')

            st.download_button(
                label=f"📥 Изтегли Архив ({period_label})",
                data=json_bytes,
                file_name=f"sklad_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
                mime="application/json"
            )
            st.success(f"🎉 Архивът за период [{period_label}] бе генериран успешно!")

    with col_bak2:
        st.markdown("#### 📤 Възстановяване на данни от архив (Restore)")
        uploaded_file = st.file_uploader("Качете JSON архив за възстановяване:", type=["json"])

        if uploaded_file is not None:
            restore_mode = st.radio("Режим на възстановяване:", [
                "Добавяне / Обновяване (Препоръчително за периодични архиви)",
                "Пълно заместване (Изтрива настоящите данни и зарежда архива)"
            ])

            if st.button("⚠️ Потвърди и Възстанови данните"):
                try:
                    data = json.load(uploaded_file)

                    if "Пълно заместване" in restore_mode:
                        cursor.execute("DELETE FROM inventory")
                        cursor.execute("DELETE FROM movement_history")

                    for comp in data.get('companies', []):
                        cursor.execute(
                            "INSERT OR REPLACE INTO companies (id, name, eik, address, mol) VALUES (?, ?, ?, ?, ?)",
                            (comp.get('id'), comp.get('name'), comp.get('eik', ''), comp.get('address', ''),
                             comp.get('mol', ''))
                        )
                    for item in data.get('inventory', []):
                        cursor.execute(
                            "INSERT OR REPLACE INTO inventory (id, company_id, name, category, quantity, min_limit, price) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (item.get('id'), item.get('company_id'), item.get('name'), item.get('category'),
                             item.get('quantity'), item.get('min_limit'), item.get('price'))
                        )
                    for move in data.get('movement_history', []):
                        cursor.execute(
                            '''INSERT OR REPLACE INTO movement_history (
                                id, company_id, item_name, action_type, quantity_change, unit_price, 
                                doc_type, doc_number, doc_date, supplier_name, supplier_eik, supplier_address, timestamp
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                            (move.get('id'), move.get('company_id'), move.get('item_name'), move.get('action_type'),
                             move.get('quantity_change'), move.get('unit_price'), move.get('doc_type'),
                             move.get('doc_number'), move.get('doc_date'), move.get('supplier_name', ''),
                             move.get('supplier_eik', ''), move.get('supplier_address', ''), move.get('timestamp'))
                        )
                    conn.commit()
                    st.success("✅ Базата данни бе възстановена / обновена успешно!")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ Грешка при възстановяване на архива: {e}")


# --- 8.9. ПРОФИЛ & ПАРОЛИ ---
elif choice == "👤 Профил & Пароли":
    st.subheader("👤 Управление на профили и пароли")

    col_p1, col_p2 = st.columns(2)

    with col_p1:
        st.markdown(f"#### 🔐 Смяна на вашата парола (`{st.session_state['username']}`)")
        with st.form("change_pass_form"):
            old_pass = st.text_input("Текуща парола", type="password")
            new_pass = st.text_input("Нова парола", type="password")
            confirm_pass = st.text_input("Потвърди новата парола", type="password")
            submit_change_pass = st.form_submit_button("🔑 Промени паролата")

            if submit_change_pass:
                cursor.execute("SELECT password FROM users WHERE username = ?", (st.session_state['username'],))
                current_db_pass = cursor.fetchone()[0]

                if hash_password(old_pass) != current_db_pass:
                    st.error("❌ Грешна текуща парола!")
                elif not new_pass.strip():
                    st.error("⚠️ Новата парола не може да бъде празна!")
                elif new_pass != confirm_pass:
                    st.error("❌ Новите пароли не съвпадат!")
                else:
                    cursor.execute("UPDATE users SET password = ? WHERE username = ?",
                                   (hash_password(new_pass), st.session_state['username']))
                    conn.commit()
                    st.success("🎉 Паролата бе променена успешно!")

    with col_p2:
        st.markdown("#### ➕ Добавяне на нов потребител")
        with st.form("add_user_form", clear_on_submit=True):
            new_username = st.text_input("Ново потребителско име")
            new_user_pass = st.text_input("Парола за новия потребител", type="password")
            submit_add_user = st.form_submit_button("👤 Добави потребител")

            if submit_add_user:
                if not new_username.strip() or not new_user_pass.strip():
                    st.error("⚠️ Попълнете потребителско име и парола!")
                else:
                    try:
                        cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)",
                                       (new_username.strip(), hash_password(new_user_pass)))
                        conn.commit()
                        st.success(f"🎉 Потребителят '{new_username.strip()}' бе добавен успешно!")
                    except sqlite3.IntegrityError:
                        st.error("❌ Потребител с такова име вече съществува!")