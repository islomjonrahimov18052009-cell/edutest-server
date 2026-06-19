from flask import Flask, request, jsonify
from flask_cors import CORS
import struct, zlib, re, base64, subprocess, tempfile, os, sys
from concurrent.futures import ThreadPoolExecutor, as_completed

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

def emf_to_png_b64(emf_data, idx=0):
    """EMF rasmni PNG base64 ga o'girish"""
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix=f'eq{idx}_')
        emf_path = os.path.join(tmpdir, 'f.emf')
        png_path = os.path.join(tmpdir, 'f.png')
        with open(emf_path, 'wb') as f:
            f.write(emf_data)
        env = os.environ.copy()
        env['HOME'] = tmpdir
        for cmd in ['libreoffice', 'soffice']:
            try:
                r = subprocess.run(
                    [cmd, '--headless', '--norestore',
                     '--convert-to', 'png', '--outdir', tmpdir, emf_path],
                    capture_output=True, timeout=20, env=env
                )
                if os.path.exists(png_path) and os.path.getsize(png_path) > 2000:
                    with open(png_path, 'rb') as f:
                        png_bytes = f.read()
                    print(f"EMF[{idx}]->PNG OK: {len(png_bytes)}b", file=sys.stderr)
                    return 'data:image/png;base64,' + base64.b64encode(png_bytes).decode()
            except Exception as e:
                print(f"EMF[{idx}] {cmd} err: {e}", file=sys.stderr)
    except Exception as e:
        print(f"EMF[{idx}] error: {e}", file=sys.stderr)
    finally:
        if tmpdir and os.path.exists(tmpdir):
            for fp in os.listdir(tmpdir):
                try: os.unlink(os.path.join(tmpdir, fp))
                except: pass
            try: os.rmdir(tmpdir)
            except: pass
    return None

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

    # EMF marker - faqat belgi, asl o'girish keyinroq parallel
    tmet_pos = rvf.find(b'TMetafile\r\n')
    if tmet_pos >= 0:
        after = rvf[tmet_pos+11:]
        if len(after) >= 8:
            emf_size = struct.unpack_from('<I', after, 0)[0]
            if emf_size > 100 and len(after) >= 4 + emf_size:
                emf_data = after[4:4+emf_size]
                return '__EMF__', emf_data  # parallel o'girish uchun

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
    emf_tasks = []  # (q_index, emf_data, task_idx)

    # 1-pass: barcha savollarni o'qi, EMF larni belgilab qo'y
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
                    emf_data_q = ri  # bytes
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

    # 2-pass: EMF larni parallel o'gir (max 4 ta bir vaqtda)
    if emf_tasks:
        print(f"Converting {len(emf_tasks)} EMFs in parallel...", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futs = {}
            for task_idx, (q_idx, emf_data) in enumerate(emf_tasks):
                f = executor.submit(emf_to_png_b64, emf_data, task_idx)
                futs[f] = q_idx
            for f in as_completed(futs):
                q_idx = futs[f]
                try:
                    b64 = f.result()
                    if b64:
                        questions[q_idx]['image'] = b64
                except Exception as e:
                    print(f"EMF future error: {e}", file=sys.stderr)

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
