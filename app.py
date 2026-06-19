 from flask import Flask, request, jsonify
from flask_cors import CORS
import struct, zlib, re, base64, subprocess, tempfile, os, sys

app = Flask(__name__)
CORS(app)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

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
    """Barcha EMF larni bitta LibreOffice session da o'girish"""
    if not emf_list:
        return {}
    
    tmpdir = tempfile.mkdtemp(prefix='edutest_emf_')
    results = {}  # idx -> base64
    
    try:
        # Barcha EMF fayllarni yozish
        emf_paths = {}
        for idx, emf_data in emf_list:
            emf_path = os.path.join(tmpdir, f'f{idx}.emf')
            with open(emf_path, 'wb') as f:
                f.write(emf_data)
            emf_paths[idx] = emf_path
        
        env = os.environ.copy()
        env['HOME'] = tmpdir
        
        # Bitta LibreOffice chaqiruvi - barcha fayllar
        all_emf_paths = list(emf_paths.values())
        
        print(f"Converting {len(all_emf_paths)} EMFs in one LO call...", file=sys.stderr)
        
        r = subprocess.run(
            ['libreoffice', '--headless', '--norestore',
             '--convert-to', 'png:draw_png_Export:{PixelWidth:1200}',
             '--outdir', tmpdir] + all_emf_paths,
            capture_output=True, timeout=240, env=env
        )
        print(f"LO rc={r.returncode} stdout={r.stdout.decode()[:200]}", file=sys.stderr)
        
        # PNG fayllarni o'qi va crop qil
        for idx, emf_path in emf_paths.items():
            png_path = emf_path.replace('.emf', '.png')
            if os.path.exists(png_path) and os.path.getsize(png_path) > 2000:
                try:
                    from PIL import Image
                    import io
                    img = Image.open(png_path).convert('RGB')
                    # Oq chegaralarni crop qilish (numpy siz)
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
                except Exception as e:
                    print(f"  crop err: {e}", file=sys.stderr)
                    with open(png_path, 'rb') as f:
                        png_bytes = f.read()
                results[idx] = 'data:image/png;base64,' + base64.b64encode(png_bytes).decode()
                print(f"  EMF[{idx}] -> {len(png_bytes)}b PNG", file=sys.stderr)
            else:
                print(f"  EMF[{idx}] -> FAILED", file=sys.stderr)
    
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

    # JPEG
    jpg_start = rvf.find(b'\xff\xd8\xff')
    if jpg_start >= 0:
        jpg_data = rvf[jpg_start:]
        end = jpg_data.rfind(b'\xff\xd9')
        if end >= 0: jpg_data = jpg_data[:end+2]
        if len(jpg_data) > 500:
            return None, 'data:image/jpeg;base64,' + base64.b64encode(jpg_data).decode()

    # PNG
    png_start = rvf.find(b'\x89PNG\r\n\x1a\n')
    if png_start >= 0:
        png_data = rvf[png_start:]
        if len(png_data) > 500:
            return None, 'data:image/png;base64,' + base64.b64encode(png_data).decode()

    # EMF (formula) - ikkita format bor
    tmet_pos = rvf.find(b'TMetafile\r\n')
    if tmet_pos >= 0:
        after = rvf[tmet_pos+11:]
        # spacing=N\r\n bo'lsa o'tkazib yubor
        if after.startswith(b'spacing='):
            nl = after.find(b'\r\n')
            after = after[nl+2:] if nl >= 0 else after
        if len(after) >= 8:
            # Format 1: 4byte_size + EMF (29_mavzu turi)
            emf_size = struct.unpack_from('<I', after, 0)[0]
            if 100 < emf_size <= len(after) - 4:
                candidate = after[4:4+emf_size]
                if candidate[:4] == b'\x01\x00\x00\x00':
                    return '__EMF__', candidate
            # Format 2: 4byte_skip + EMF (K2_1-mavzu turi)
            candidate2 = after[4:]
            if len(candidate2) > 100 and candidate2[:4] == b'\x01\x00\x00\x00':
                return '__EMF__', candidate2
            # Format 3: to'g'ridan EMF
            if after[:4] == b'\x01\x00\x00\x00':
                return '__EMF__', after

    # UTF-16 matn
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

def parse_questions(xml_text, data):
    name_m = re.search(r'<Name>([\s\S]*?)</Name>', xml_text)
    topic = name_m.group(1).strip() if name_m else 'Test'
    qta_m = re.search(r'<QuestionsToAsk>(\d+)</QuestionsToAsk>', xml_text)
    questions_to_ask = int(qta_m.group(1)) if qta_m else 20

    blocks = re.findall(r'<QuestionBlock[^>]*>([\s\S]*?)</QuestionBlock>', xml_text)
    questions = []
    emf_tasks = []  # (q_idx, emf_bytes)

    for i, block in enumerate(blocks):
        type_m = re.search(r'<QuestionTypeName>(.*?)</QuestionTypeName>', block)
        qtype = type_m.group(1).strip() if type_m else 'MultipleChoice'
        if qtype not in ('MultipleChoice', 'MultipleResponse'): continue

        # Savol
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

        # Javoblar
        opts, corr = [], []
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
            if a_rvf_m:
                a_pos = int(a_rvf_m.group(1))
                a_len = int(a_rvf_m.group(2))
                a_rt, _ = read_rvf(data, a_pos, a_len)
                if a_rt and a_rt != '__EMF__' and len(a_rt) > len(a_plain):
                    a_text = a_rt
            if a_text:
                opts.append(a_text)
                if am.group(1) == 'Yes':
                    corr.append(len(opts)-1)

        if len(opts) >= 2 and corr:
            q_obj = {
                'id': i, 'subject': 'math', 'topic': topic,
                'text': q_text or '(Rasm)',
                'options': opts, 'correct': corr,
            }
            if img_b64:
                q_obj['image'] = img_b64
            q_idx = len(questions)
            questions.append(q_obj)
            if emf_data_q:
                emf_tasks.append((q_idx, emf_data_q))

    # Barcha EMF larni bitta LO session da o'gir
    if emf_tasks:
        emf_results = convert_all_emfs(emf_tasks)
        for q_idx, b64 in emf_results.items():
            questions[q_idx]['image'] = b64

    img_count = sum(1 for q in questions if q.get('image'))
    print(f"Done: {len(questions)} questions, {img_count} images", file=sys.stderr)
    return {'topic': topic, 'questions': questions, 'questionsToAsk': questions_to_ask}

@app.route('/parse', methods=['POST', 'OPTIONS'])
def parse():
    if request.method == 'OPTIONS':
        return '', 200
    try:
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
