from flask import Flask, request, jsonify
from flask_cors import CORS
import struct, zlib, re, base64, subprocess, tempfile, os, sys, io, json, time, uuid
import requests

app = Flask(__name__)
CORS(app)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

# GROQ_API_KEY endi bu yerda, SERVER MUHIT OZGARUVCHISI sifatida saqlanadi -
# oldin index.html ichida ochiq turgan edi, hamma korishi mumkin edi. Render
# Dashboard > Environment da GROQ_API_KEY nomli ozgaruvchi qoshish kerak.
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')

# Supabase jadvaliga (edutest_store) endi FAQAT shu server orqali, "service
# role" kaliti bilan kiriladi - bu kalit RLS'ni chetlab otadi va HECH QACHON
# brauzerga chiqarilmaydi. Render Dashboard > Environment'da SUPABASE_URL va
# SUPABASE_SERVICE_KEY nomli ozgaruvchilarni qoshish kerak (Supabase Dashboard
# > Settings > API > "service_role" kaliti - "anon" kaliti EMAS).
SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://ykpegjtexjwddsfgwwpw.supabase.co')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
DB_KEY = 'edutest_v3'

@app.route('/db_get', methods=['GET', 'OPTIONS'])
def db_get():
    if request.method == 'OPTIONS':
        return '', 200
    if not SUPABASE_SERVICE_KEY:
        return jsonify({'error': 'Server sozlanmagan (SUPABASE_SERVICE_KEY yoq)'}), 500
    try:
        r = requests.get(
            f'{SUPABASE_URL}/rest/v1/edutest_store',
            params={'key': f'eq.{DB_KEY}', 'select': 'value'},
            headers={
                'apikey': SUPABASE_SERVICE_KEY,
                'Authorization': 'Bearer ' + SUPABASE_SERVICE_KEY,
            },
            timeout=15
        )
        if r.status_code >= 400:
            return jsonify({'error': f'Supabase xato: {r.status_code}'}), 502
        data = r.json()
        if data and len(data) > 0:
            return jsonify({'value': data[0]['value']})
        return jsonify({'value': None})
    except Exception as e:
        print(f"db_get error: {e}", file=sys.stderr)
        return jsonify({'error': str(e)}), 500

@app.route('/db_save', methods=['POST', 'OPTIONS'])
def db_save():
    if request.method == 'OPTIONS':
        return '', 200
    if not SUPABASE_SERVICE_KEY:
        return jsonify({'error': 'Server sozlanmagan (SUPABASE_SERVICE_KEY yoq)'}), 500
    try:
        body = request.get_json(force=True, silent=True) or {}
        value = body.get('value')
        if value is None:
            return jsonify({'error': 'No value'}), 400
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/edutest_store',
            headers={
                'apikey': SUPABASE_SERVICE_KEY,
                'Authorization': 'Bearer ' + SUPABASE_SERVICE_KEY,
                'Content-Type': 'application/json',
                'Prefer': 'resolution=merge-duplicates',
            },
            json={'key': DB_KEY, 'value': value},
            timeout=15
        )
        if r.status_code >= 400:
            return jsonify({'error': f'Supabase xato: {r.status_code} {r.text[:200]}'}), 502
        return jsonify({'ok': True})
    except Exception as e:
        print(f"db_save error: {e}", file=sys.stderr)
        return jsonify({'error': str(e)}), 500

@app.route('/ai_check', methods=['POST', 'OPTIONS'])
def ai_check():
    """Frontend endi Groq'ga togridan-togri emas, shu endpoint orqali murojaat
    qiladi - shunda haqiqiy API kalit brauzerga hech qachon chiqmaydi."""
    if request.method == 'OPTIONS':
        return '', 200
    if not GROQ_API_KEY:
        return jsonify({'error': "AI tekshirish xizmati sozlanmagan (serverda GROQ_API_KEY yoq)"}), 500
    try:
        body = request.get_json(force=True, silent=True) or {}
        messages = body.get('messages')
        if not messages:
            return jsonify({'error': 'No messages'}), 400
        r = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + GROQ_API_KEY},
            json={
                'model': body.get('model', 'llama-3.3-70b-versatile'),
                'messages': messages,
                'max_tokens': body.get('max_tokens', 1000),
                'temperature': body.get('temperature', 0.1),
            },
            timeout=60
        )
        return jsonify(r.json()), r.status_code
    except Exception as e:
        print(f"ai_check error: {e}", file=sys.stderr)
        return jsonify({'error': str(e)}), 500

def find_xml(data):
    for i in range(len(data)-6, max(0, len(data)-2000000), -1):
        if i+1 >= len(data): continue
        b0, b1 = data[i], data[i+1]
        if b0 == 0x78 and b1 in (0x01, 0x9c, 0xda, 0x5e):
            for method in [
                lambda c: zlib.decompress(c),
                lambda c: zlib.decompress(c, 47),
                lambda c: zlib.decompress(c[2:], -15),
                lambda c: zlib.decompress(c, -15),
            ]:
                try:
                    out = method(data[i:i+1000000])
                    if b'QuestionBlock' in out or b'<?xml' in out:
                        return out.decode('utf-8', errors='replace')
                except: pass
    return None

def convert_all_emfs(emf_list):
    """Barcha EMF larni bitta LibreOffice session da o'girish.
    emf_list: [(idx, emf_bytes), ...] - idx dunyoning istalgan raqami bolishi mumkin"""
    if not emf_list:
        return {}

    tmpdir = tempfile.mkdtemp(prefix='edutest_emf_')
    results = {}  # idx -> base64

    try:
        emf_paths = {}
        for idx, emf_data in emf_list:
            emf_path = os.path.join(tmpdir, f'f{idx}.emf')
            with open(emf_path, 'wb') as f:
                f.write(emf_data)
            emf_paths[idx] = emf_path

        env = os.environ.copy()
        env['HOME'] = tmpdir

        all_emf_paths = list(emf_paths.values())

        print(f"Converting {len(all_emf_paths)} EMFs in batches (1 LO session)...", file=sys.stderr)
        # MUHIM: Render "Free" tarifi atigi 512 MB xotira beradi. Bitta
        # LibreOffice chaqiruvida juda kop fayl bolsa, xotira tugab (OOM)
        # butun server qulab tushishi mumkin edi. Shuning uchun BATCH kichik
        # tutilgan - sekinroq, lekin barqaror.
        BATCH = 15
        for b_start in range(0, len(all_emf_paths), BATCH):
            batch = all_emf_paths[b_start:b_start+BATCH]
            r = subprocess.run(
                ['libreoffice', '--headless', '--norestore',
                 '--convert-to', 'png:draw_png_Export:{PixelWidth:550}',
                 '--outdir', tmpdir] + batch,
                capture_output=True, timeout=300, env=env
            )
            print(f"Batch {b_start//BATCH+1}: rc={r.returncode}", file=sys.stderr)

        for idx, emf_path in emf_paths.items():
            png_path = emf_path.replace('.emf', '.png')
            if os.path.exists(png_path) and os.path.getsize(png_path) > 2000:
                try:
                    from PIL import Image
                    import io
                    img = Image.open(png_path).convert('RGB')
                    bbox = img.point(lambda x: 0 if x > 240 else 255).convert('L').getbbox()
                    if bbox:
                        pad = 15
                        w, h = img.size
                        bbox = (max(0,bbox[0]-pad), max(0,bbox[1]-pad),
                                min(w,bbox[2]+pad), min(h,bbox[3]+pad))
                        img = img.crop(bbox)
                    buf = io.BytesIO()
                    img.save(buf, format='PNG', optimize=True)
                    png_bytes = buf.getvalue()
                    img.close()
                    buf.close()
                except Exception as e:
                    print(f"  crop err: {e}", file=sys.stderr)
                    with open(png_path, 'rb') as f:
                        png_bytes = f.read()
                results[idx] = 'data:image/png;base64,' + base64.b64encode(png_bytes).decode()
                del png_bytes
            else:
                print(f"  EMF[{idx}] -> FAILED", file=sys.stderr)
            # Rasm faylini darhol ochirib, diskni ham bosh qilamiz
            try: os.unlink(png_path)
            except: pass
            try: os.unlink(emf_path)
            except: pass
        import gc
        gc.collect()

    except subprocess.TimeoutExpired:
        print("LO timeout!", file=sys.stderr)
    except Exception as e:
        print(f"LO error: {e}", file=sys.stderr)
    finally:
        for fp in os.listdir(tmpdir):
            try: os.unlink(os.path.join(tmpdir, fp))
            except: pass
        try: os.rmdir(tmpdir)
        except: pass

    return results

def read_rvf(data, pos, length):
    if length <= 0 or pos <= 0:
        return None, None
    rvf = data[pos:pos+length]

    jpg_start = rvf.find(b'\xff\xd8\xff')
    if jpg_start >= 0:
        jpg_data = rvf[jpg_start:]
        end = jpg_data.rfind(b'\xff\xd9')
        if end >= 0: jpg_data = jpg_data[:end+2]
        if len(jpg_data) > 500:
            return None, 'data:image/jpeg;base64,' + base64.b64encode(jpg_data).decode()

    png_start = rvf.find(b'\x89PNG\r\n\x1a\n')
    if png_start >= 0:
        png_data = rvf[png_start:]
        if len(png_data) > 500:
            return None, 'data:image/png;base64,' + base64.b64encode(png_data).decode()

    tmet_pos = rvf.find(b'TMetafile\r\n')
    if tmet_pos >= 0:
        after = rvf[tmet_pos+11:]
        # TMetafile'dan keyin "spacing=", "width=", "height=" kabi bir nechta
        # metama'lumot qatorlari kelishi mumkin (obyekt turiga qarab har xil).
        # Hammasini o'tkazib yuboramiz, faqat binary EMF boshlanguncha.
        while True:
            nl = after.find(b'\r\n')
            if nl < 0 or nl > 40:
                break
            line = after[:nl]
            if b'=' in line and all(32 <= c < 127 for c in line):
                after = after[nl+2:]
            else:
                break
        if len(after) >= 8:
            emf_size = struct.unpack_from('<I', after, 0)[0]
            if 100 < emf_size <= len(after) - 4:
                candidate = after[4:4+emf_size]
                if candidate[:4] == b'\x01\x00\x00\x00':
                    return '__EMF__', candidate
            candidate2 = after[4:]
            if len(candidate2) > 100 and candidate2[:4] == b'\x01\x00\x00\x00':
                emf_hdr_size = struct.unpack_from('<I', candidate2, 4)[0]
                if 100 < emf_hdr_size <= len(candidate2):
                    return '__EMF__', candidate2[:emf_hdr_size]
                return '__EMF__', candidate2
            if after[:4] == b'\x01\x00\x00\x00':
                return '__EMF__', after

    lines = rvf.split(b'\r\n')
    if len(lines) >= 3:
        text_part = b'\r\n'.join(lines[2:])
        try:
            text = text_part.decode('utf-16-le', errors='replace').strip()
            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text).strip()
            readable = sum(1 for c in text if c.isprintable() or c in '\n\r\t')
            if len(text) > 2 and readable / max(len(text), 1) > 0.7:
                return text, None
        except: pass

    return None, None

def extract_questions_raw(xml_text, data):
    """parse_questions bilan bir xil, lekin EMF larni konvertatsiya QILMAYDI -
    faqat xom emf_tasks royxatini qaytaradi. Bu bir nechta faylni birlashtirib,
    hammasini BITTA LibreOffice chaqiruvida ogirish uchun kerak (tezlik uchun)."""
    name_m = re.search(r'<Name>([\s\S]*?)</Name>', xml_text)
    topic = name_m.group(1).strip() if name_m else 'Test'
    qta_m = re.search(r'<QuestionsToAsk>(\d+)</QuestionsToAsk>', xml_text)
    questions_to_ask = int(qta_m.group(1)) if qta_m else 20

    blocks = re.findall(r'<QuestionBlock[^>]*>([\s\S]*?)</QuestionBlock>', xml_text)
    questions = []
    emf_tasks = []  # {'kind':'q'|'a', 'q_idx':i, 'opt_idx':j, 'emf':bytes}

    for i, block in enumerate(blocks):
        type_m = re.search(r'<QuestionTypeName>(.*?)</QuestionTypeName>', block)
        qtype = type_m.group(1).strip() if type_m else 'MultipleChoice'
        if qtype not in ('MultipleChoice', 'MultipleResponse'): continue

        content_m = re.search(r'<Content>([\s\S]*?)</Content>', block)
        q_text = ''
        img_b64 = None
        emf_data_q = None

        if content_m:
            content = content_m.group(1)
            plain_m = re.search(r'<PlainText>([\s\S]*?)</PlainText>', content)
            plain = plain_m.group(1).strip() if plain_m else ''
            rvf_m = re.search(
                r'<RVFStoredPos>(\d+)</RVFStoredPos>\s*<RVFStoredLen>(\d+)</RVFStoredLen>',
                content)
            if rvf_m:
                rp, rl = int(rvf_m.group(1)), int(rvf_m.group(2))
                rt, ri = read_rvf(data, rp, rl)
                if rt == '__EMF__':
                    q_text = plain or '(formula)'
                    emf_data_q = ri
                elif ri:
                    q_text = plain
                    img_b64 = ri
                elif rt:
                    q_text = rt
                else:
                    q_text = plain
            else:
                q_text = plain

        if not q_text and not img_b64 and not emf_data_q:
            continue

        opts, corr = [], []
        ans_emf_tasks = []
        for am in re.finditer(
            r'<Answer\s+IsCorrect="(Yes|No)"[\s\S]*?<Content>([\s\S]*?)</Content>',
            block):
            ac = am.group(2)
            ap = re.search(r'<PlainText>([\s\S]*?)</PlainText>', ac)
            a_plain = ap.group(1).strip() if ap else ''
            a_rvf_m = re.search(
                r'<RVFStoredPos>(\d+)</RVFStoredPos>\s*<RVFStoredLen>(\d+)</RVFStoredLen>',
                ac)
            a_text = a_plain
            a_emf = None
            if a_rvf_m:
                a_pos = int(a_rvf_m.group(1))
                a_len = int(a_rvf_m.group(2))
                a_rt, a_ri = read_rvf(data, a_pos, a_len)
                if a_rt == '__EMF__':
                    a_emf = a_ri
                    a_text = a_plain or '__IMG_PENDING__'
                elif a_ri:
                    a_text = a_ri
                elif a_rt and len(a_rt) > len(a_plain):
                    a_text = a_rt
            if a_text is not None or a_emf:
                opt_idx = len(opts)
                opts.append(a_text if a_text else '')
                if am.group(1) == 'Yes':
                    corr.append(opt_idx)
                if a_emf:
                    ans_emf_tasks.append((opt_idx, a_emf))

        if len(opts) >= 2 and corr:
            q_obj = {
                'id': i, 'subject': 'math', 'topic': topic,
                'text': q_text or '(Rasm)',
                'options': opts, 'correct': corr,
                'isMulti': (qtype == 'MultipleResponse') or (len(corr) > 1),
            }
            if img_b64:
                q_obj['image'] = img_b64
            q_idx = len(questions)
            questions.append(q_obj)
            if emf_data_q:
                emf_tasks.append({'kind': 'q', 'q_idx': q_idx, 'emf': emf_data_q})
            for opt_idx, a_emf in ans_emf_tasks:
                emf_tasks.append({'kind': 'a', 'q_idx': q_idx, 'opt_idx': opt_idx, 'emf': a_emf})

    return topic, questions, emf_tasks, questions_to_ask

def resolve_emf_tasks(questions, emf_tasks):
    """Bitta fayl uchun: emf_tasks larni konvertatsiya qilib, questions ichiga joylaydi"""
    if not emf_tasks:
        return
    emf_list = [(idx, t['emf']) for idx, t in enumerate(emf_tasks)]
    emf_results = convert_all_emfs(emf_list)
    for idx, b64 in emf_results.items():
        t = emf_tasks[idx]
        if t['kind'] == 'q':
            questions[t['q_idx']]['image'] = b64
        else:
            opts = questions[t['q_idx']]['options']
            if t['opt_idx'] < len(opts):
                opts[t['opt_idx']] = b64

def parse_questions(xml_text, data):
    topic, questions, emf_tasks, questions_to_ask = extract_questions_raw(xml_text, data)
    resolve_emf_tasks(questions, emf_tasks)
    img_count = sum(1 for q in questions if q.get('image'))
    print(f"Done: {len(questions)} questions, {img_count} images", file=sys.stderr)
    return {'topic': topic, 'questions': questions, 'questionsToAsk': questions_to_ask}

@app.route('/parse', methods=['POST', 'OPTIONS'])
def parse():
    if request.method == 'OPTIONS':
        return '', 200
    try:
        content_type = (request.content_type or '')
        if 'application/json' in content_type:
            body = request.get_json(force=True, silent=True) or {}
            b64 = body.get('data', '')
            if not b64:
                return jsonify({'error': 'No data'}), 400
            data = base64.b64decode(b64)
        else:
            data = request.data
        if not data:
            return jsonify({'error': 'No data'}), 400
        print(f"Received: {len(data)} bytes", file=sys.stderr)
        xml_text = find_xml(data)
        if not xml_text:
            return jsonify({'error': 'XML not found'}), 400
        result = parse_questions(xml_text, data)
        return jsonify(result)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return jsonify({'error': str(e)}), 500


# ─── BOSHQA FAYL TURLARI (Word/PDF/TXT) - AI ORQALI TAHLIL ────────────────
# EasyQuizzy .exe fayllar QATIY strukturaga ega bolgani uchun regex bilan
# ochib olinadi. Lekin Word/PDF/TXT fayllar HAR XIL formatda yozilishi
# mumkin - shuning uchun bu yerda: 1) fayldan matn+rasmlarni (joylashuv
# tartibida, rasm ornida [RASM_N] belgisi bilan) chiqarib olamiz, 2) shu
# matnni AI'ga (Groq) berib, savol/variant/togri-javob strukturasini
# chiqarishni soraymiz, 3) AI qaytargan [RASM_N] belgilarni haqiqiy rasmga
# almashtiramiz.

def extract_docx_content(data):
    """DOCX fayldan paragraflar tartibida matn va rasm ornlarini chiqaradi.
    MUHIM: qalin (bold) va tagiga chizilgan (underline) matnni **belgi**
    bilan saqlab qoladi - aks holda ustoz togri javobni qalin qilib
    belgilagan bolsa ham, bu ma'lumot AI'ga yetib bormay, hech qanday togri
    javob topilmas edi.
    Qaytaradi: (matn, {placeholder: (rasm_bytes, ext)})"""
    import docx
    doc = docx.Document(io.BytesIO(data))
    images = {}
    img_counter = [0]
    ns_a = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
    ns_r = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
    lines = []

    def get_image_bytes(rId):
        try:
            part = doc.part.related_parts[rId]
            return part.blob
        except Exception:
            return None

    for para in doc.paragraphs:
        line_parts = []
        for run in para.runs:
            if run.text:
                t = run.text
                is_marked = bool(run.bold) or bool(run.underline) or bool(run.font.highlight_color)
                if is_marked and t.strip():
                    t = '**' + t + '**'
                line_parts.append(t)
            for blip in run._element.findall('.//' + ns_a + 'blip'):
                rId = blip.get(ns_r + 'embed')
                if rId:
                    img_bytes = get_image_bytes(rId)
                    if img_bytes:
                        img_counter[0] += 1
                        ph = f'[RASM_{img_counter[0]}]'
                        images[ph] = (img_bytes, 'png')
                        line_parts.append(ph)
        if line_parts:
            lines.append(''.join(line_parts))
    return '\n'.join(lines), images


def extract_pdf_content(data):
    """PDF fayldan sahifa boyicha matn va rasmlarni Y-koordinata (joylashuv)
    tartibida chiqaradi. Qalin (bold) matn ham **belgi** bilan saqlanadi -
    PyMuPDF span flags orqali aniqlanadi (bit4 = bold)."""
    import fitz
    doc = fitz.open(stream=data, filetype='pdf')
    images = {}
    img_counter = [0]
    lines = []
    for page in doc:
        blocks = page.get_text('dict').get('blocks', [])
        items = []
        for b in blocks:
            if b.get('type') == 0:
                text = ''
                for line in b.get('lines', []):
                    for span in line.get('spans', []):
                        span_text = span.get('text', '')
                        is_bold = bool(span.get('flags', 0) & 16) or ('bold' in span.get('font', '').lower())
                        if is_bold and span_text.strip():
                            span_text = '**' + span_text + '**'
                        text += span_text
                    text += '\n'
                if text.strip():
                    items.append((b['bbox'][1], text.strip()))
            elif b.get('type') == 1:
                img_bytes = b.get('image')
                ext = b.get('ext', 'png')
                if img_bytes:
                    img_counter[0] += 1
                    ph = f'[RASM_{img_counter[0]}]'
                    images[ph] = (img_bytes, ext)
                    items.append((b['bbox'][1], ph))
        items.sort(key=lambda x: x[0])
        for _, content in items:
            lines.append(content)
    doc.close()
    return '\n'.join(lines), images


def extract_content_from_file(filename, data):
    ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
    if ext == 'docx':
        return extract_docx_content(data)
    elif ext == 'pdf':
        return extract_pdf_content(data)
    elif ext == 'txt':
        return data.decode('utf-8', errors='replace'), {}
    return None, {}


def parse_text_with_ai(text):
    if not GROQ_API_KEY:
        raise Exception('AI xizmati sozlanmagan (serverda GROQ_API_KEY yoq)')
    MAX_CHARS = 40000
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    prompt = (
        "Quyidagi hujjat matnidan test savollarini JSON korinishida chiqarib ol.\n"
        "QATIY QOIDALAR:\n"
        "- FAQAT JSON qaytar, boshqa hech qanday matn, izoh yoki markdown yozma.\n"
        "- Format: {\"topic\": \"mavzu nomi\", \"questions\": [{\"text\": \"savol matni\", "
        "\"options\": [\"variant1\",\"variant2\"], \"correct\": [0], \"isMulti\": false}]}\n"
        "- \"correct\" - togri javob(lar)ning options ichidagi index(lar)i (0 dan boshlanadi).\n"
        "- Agar bir nechta togri javob bolsa, isMulti:true va correct bir nechta index bolsin.\n"
        "- MUHIM: matnda **ikki yulduzcha orasidagi** qism - bu asl hujjatda QALIN, TAGIGA "
        "CHIZILGAN yoki BELGILANGAN (highlight) matnni bildiradi. O'qituvchilar odatda TOGRI "
        "JAVOBNI aynan shu tarzda (qalin qilib) belgilaydilar - shuning uchun variant matni "
        "**qalin** bolsa, o'sha variant TOGRI JAVOB deb hisobla. Variantning FAQAT bir qismi "
        "qalin bolsa ham (masalan raqami yoki bitta sozi), butun variant togri hisoblanadi.\n"
        "- Agar hech qanday variant qalin qilinmagan VA \"Javob:\", \"togri:\" kabi aniq izoh "
        "ham yoq bolsa, o'sha savolni OTKAZIB YUBOR - taxmin qilma.\n"
        "- JSON javobingda **belgilarini OLIB TASHLA (ular faqat sen uchun ishora, natijaviy "
        "matnda bolmasligi kerak) - masalan \"**4**\" emas, faqat \"4\" deb yoz.\n"
        "- Matnda [RASM_N] kabi belgilar bolishi mumkin - bular rasm ornini bildiradi. Bu "
        "belgilarni ANIQ ozgarishsiz saqlab qol. Agar savol yoki variant BUTUNLAY rasmdan "
        "iborat bolsa, oshu maydonda FAQAT placeholder'ni yoz (masalan faqat \"[RASM_2]\"), "
        "boshqa matn qoshma.\n"
        "- \"topic\" - hujjat sarlavhasi yoki mazmuniga mos qisqa nom.\n\n"
        "Matn:\n" + text
    )
    r = requests.post(
        'https://api.groq.com/openai/v1/chat/completions',
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + GROQ_API_KEY},
        json={
            'model': 'llama-3.3-70b-versatile',
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': 8000,
            'temperature': 0.1,
            'response_format': {'type': 'json_object'},
        },
        timeout=90,
    )
    result = r.json()
    if 'error' in result:
        raise Exception(str(result['error']))
    content = result['choices'][0]['message']['content'].strip()
    if content.startswith('```'):
        content = re.sub(r'^```[a-zA-Z]*\n?', '', content)
        content = re.sub(r'```\s*$', '', content)
    return json.loads(content)


def substitute_image_placeholders(questions, images):
    def resolve(s):
        if not isinstance(s, str):
            return s
        m = re.fullmatch(r'\[RASM_(\d+)\]', s.strip())
        if not m:
            return s
        ph = f'[RASM_{m.group(1)}]'
        if ph not in images:
            return s
        img_bytes, ext = images[ph]
        mime = 'jpeg' if ext.lower() in ('jpg', 'jpeg') else 'png'
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(img_bytes)).convert('RGB')
            buf = io.BytesIO()
            im.save(buf, format='PNG', optimize=True)
            img_bytes = buf.getvalue()
            mime = 'png'
        except Exception:
            pass
        return f'data:image/{mime};base64,' + base64.b64encode(img_bytes).decode()

    for q in questions:
        qtext = (q.get('text') or '').strip()
        resolved = resolve(qtext)
        if resolved != qtext:
            q['img'] = resolved
            q['text'] = ''
        q['options'] = [resolve(o) for o in q.get('options', [])]
    return questions


@app.route('/parse_document', methods=['POST', 'OPTIONS'])
def parse_document():
    """Word (.docx), PDF (.pdf) va oddiy matn (.txt) fayllardan savol
    import qilish - AI (Groq) yordamida erkin formatdagi matnni
    strukturaga solib beradi."""
    if request.method == 'OPTIONS':
        return '', 200
    try:
        body = request.get_json(force=True, silent=True) or {}
        b64 = body.get('data', '')
        filename = body.get('filename', 'fayl')
        if not b64:
            return jsonify({'error': 'No data'}), 400
        data = base64.b64decode(b64)
        text, images = extract_content_from_file(filename, data)
        if text is None:
            return jsonify({'error': "Fayl turi qollab-quvvatlanmaydi (.docx, .pdf, .txt qollab-quvvatlanadi)"}), 400
        if not text.strip():
            return jsonify({'error': 'Fayldan matn topilmadi'}), 400
        parsed = parse_text_with_ai(text)
        questions = parsed.get('questions', [])
        if not questions:
            return jsonify({'error': "Fayldan hech qanday savol aniqlanmadi (togri javob belgilanmagan bolishi mumkin)"}), 400
        questions = substitute_image_placeholders(questions, images)
        for i, q in enumerate(questions):
            q['id'] = f'{int(time.time()*1000)}_{i}_{uuid.uuid4().hex[:6]}'
            q.setdefault('subject', 'math')
            if 'isMulti' not in q:
                q['isMulti'] = len(q.get('correct', [])) > 1
        topic = parsed.get('topic') or filename.rsplit('.', 1)[0]
        img_count = sum(1 for q in questions if q.get('img'))
        print(f"parse_document: {filename} -> {len(questions)} savol, {img_count} rasm", file=sys.stderr)
        return jsonify({'topic': topic, 'questions': questions, 'questionsToAsk': len(questions)})
    except Exception as e:
        print(f"parse_document error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return jsonify({'error': str(e)}), 500


@app.route('/parse_batch', methods=['POST', 'OPTIONS'])
def parse_batch():
    """Bir nechta faylni BIR SO'ROVDA qabul qiladi va BARCHA formulalarni
    faqat BITTA LibreOffice sessiyasida o'giradi. Eski (sinxron) versiya -
    Render'ning uzoq sorovlarni majburan uzib qoyishi sababli endi
    ishlatilmaydi, lekin orqaga moslik uchun qoldirilgan."""
    if request.method == 'OPTIONS':
        return '', 200
    try:
        body = request.get_json(force=True, silent=True) or {}
        files = body.get('files', [])
        if not files:
            return jsonify({'error': 'No files'}), 400
        file_results, file_emf_tasks = _process_files_raw(files)
        _resolve_batch_emfs(file_results, file_emf_tasks)
        return jsonify({'results': file_results})
    except Exception as e:
        print(f"Batch error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return jsonify({'error': str(e)}), 500


def _process_files_raw(files):
    file_results = []
    file_emf_tasks = []
    for f in files:
        fname = f.get('filename', 'fayl')
        try:
            data = base64.b64decode(f.get('data', ''))
            xml_text = find_xml(data)
            if not xml_text:
                file_results.append({'filename': fname, 'error': 'XML not found'})
                file_emf_tasks.append([])
                continue
            topic, questions, emf_tasks, qta = extract_questions_raw(xml_text, data)
            file_results.append({'filename': fname, 'topic': topic, 'questions': questions, 'questionsToAsk': qta})
            file_emf_tasks.append(emf_tasks)
        except Exception as e:
            print(f"  {fname}: xato {e}", file=sys.stderr)
            file_results.append({'filename': fname, 'error': str(e)})
            file_emf_tasks.append([])
    return file_results, file_emf_tasks


def _resolve_batch_emfs(file_results, file_emf_tasks, job_id=None):
    global_list = []
    global_map = []
    for fi, tasks in enumerate(file_emf_tasks):
        for t in tasks:
            global_list.append((len(global_list), t['emf']))
            global_map.append((fi, t))
    total = len(global_list)
    if not total:
        return
    if job_id:
        JOBS[job_id]['progress'] = f'0/{total} rasm/formula ogirilmoqda...'

    # Kattaroq royxatlarni kichikroq boliklarga bolib ishlaymiz - shunda
    # foydalanuvchi progressni real vaqtda kora oladi (qotib qolganday
    # tuyulmasligi uchun), va bitta LibreOffice chaqiruvi haddan tashqari
    # katta bolib ketmaydi.
    # MUHIM: Render "Free" tarifi (512 MB RAM, 0.1 vCPU) uchun bu qiymat
    # ANIQ kamaytirilgan - avval 90 edi, endi 30. Kattaroq CHUNK bir vaqtning
    # ozida juda kop EMF'ni xotiraga yuklab, OOM (xotira tugashi) sabab
    # butun serverni qulatib qoyishi mumkin edi.
    CHUNK = 30
    for start in range(0, total, CHUNK):
        chunk = global_list[start:start+CHUNK]
        chunk_results = convert_all_emfs(chunk)
        for gidx, b64 in chunk_results.items():
            fi, t = global_map[gidx]
            res = file_results[fi]
            if 'questions' not in res:
                continue
            if t['kind'] == 'q':
                res['questions'][t['q_idx']]['image'] = b64
            else:
                opts = res['questions'][t['q_idx']]['options']
                if t['opt_idx'] < len(opts):
                    opts[t['opt_idx']] = b64
        done = min(start+CHUNK, total)
        if job_id:
            JOBS[job_id]['progress'] = f'{done}/{total} rasm/formula ogirildi...'
        print(f"  EMF progress: {done}/{total}", file=sys.stderr)
    return


# ─── FON VAZIFA (BACKGROUND JOB) TIZIMI ────────────────────────────────────
# Render.com (va boshqa hosting'lar) uzoq davom etadigan HTTP sorovlarni
# ozi majburan uzib qoyadi (odatda 30-100 soniyadan keyin), garchi bizning
# kod hali ishlab turgan bolsa ham. Buni chetlab otish uchun: katta ishni
# ORQA FONDA (alohida thread'da) qilamiz, brauzer esa tez-tez "tayyor
# bo'ldimi?" deb sorab turadi (polling). Har bir sorovning ozi tez
# (sub-sekund) bolgani uchun Render uni hech qachon uzib qoymaydi.
import threading, uuid

JOBS = {}  # job_id -> {'status':'processing'|'done'|'error', 'progress':str, 'results':[...], 'error':str}

def _run_batch_job(job_id, files):
    try:
        JOBS[job_id]['progress'] = f'0/{len(files)} fayl oqildi'
        file_results = []
        file_emf_tasks = []
        for i, f in enumerate(files):
            fname = f.get('filename', 'fayl')
            try:
                data = base64.b64decode(f.get('data', ''))
                xml_text = find_xml(data)
                if not xml_text:
                    file_results.append({'filename': fname, 'error': 'XML not found'})
                    file_emf_tasks.append([])
                else:
                    topic, questions, emf_tasks, qta = extract_questions_raw(xml_text, data)
                    file_results.append({'filename': fname, 'topic': topic, 'questions': questions, 'questionsToAsk': qta})
                    file_emf_tasks.append(emf_tasks)
            except Exception as e:
                print(f"  {fname}: xato {e}", file=sys.stderr)
                file_results.append({'filename': fname, 'error': str(e)})
                file_emf_tasks.append([])
            JOBS[job_id]['progress'] = f'{i+1}/{len(files)} fayl oqildi'

        _resolve_batch_emfs(file_results, file_emf_tasks, job_id)

        JOBS[job_id]['status'] = 'done'
        JOBS[job_id]['results'] = file_results
        JOBS[job_id]['progress'] = 'Tugadi'
        ok = sum(1 for r in file_results if 'questions' in r)
        print(f"Job {job_id}: tugadi, {ok}/{len(files)} muvaffaqiyatli", file=sys.stderr)
    except Exception as e:
        print(f"Job {job_id} error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        JOBS[job_id]['status'] = 'error'
        JOBS[job_id]['error'] = str(e)


@app.route('/parse_batch_start', methods=['POST', 'OPTIONS'])
def parse_batch_start():
    if request.method == 'OPTIONS':
        return '', 200
    body = request.get_json(force=True, silent=True) or {}
    files = body.get('files', [])
    if not files:
        return jsonify({'error': 'No files'}), 400
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {'status': 'processing', 'progress': 'Boshlanmoqda...'}
    JOB_TIMESTAMPS[job_id] = _time.time()
    _cleanup_stale_jobs()
    t = threading.Thread(target=_run_batch_job, args=(job_id, files), daemon=True)
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/parse_batch_status/<job_id>', methods=['GET'])
def parse_batch_status(job_id):
    _cleanup_stale_jobs()
    job = JOBS.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    resp = {'status': job['status'], 'progress': job.get('progress', '')}
    if job['status'] == 'done':
        resp['results'] = job['results']
        # Natija olib bolindi - xotirani bosh qilish uchun jobni ochiramiz.
        # (Rasmlar bilan togla katta bolgani uchun, xotirada qoldirib
        # qoyish serverni "toldirib" qoyishi mumkin edi.)
        JOBS.pop(job_id, None)
        JOB_TIMESTAMPS.pop(job_id, None)
    if job['status'] == 'error':
        resp['error'] = job.get('error', 'Nomalum xato')
        JOBS.pop(job_id, None)
        JOB_TIMESTAMPS.pop(job_id, None)
    return jsonify(resp)

# Xavfsizlik uchun: agar biror sababdan mijoz natijani hech qachon
# so'ramasa (masalan brauzer yopilib qolsa), 2 soatdan keyin eski
# joblarni avtomatik tozalaymiz - xotira sekin-asta toldirilmasin.
import time as _time
JOB_TIMESTAMPS = {}
def _cleanup_stale_jobs():
    now = _time.time()
    stale = [jid for jid, ts in list(JOB_TIMESTAMPS.items()) if now - ts > 7200]
    for jid in stale:
        JOBS.pop(jid, None)
        JOB_TIMESTAMPS.pop(jid, None)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
