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

def read_rvf(data, pos, length):
    """RVF blokidan matn yoki rasm o'qish"""
    if length <= 0 or pos <= 0:
        return None, None
    
    rvf = data[pos:pos+length]
    
    # JPEG tekshir
    jpg_start = rvf.find(b'\xff\xd8\xff')
    if jpg_start >= 0:
        jpg_data = rvf[jpg_start:]
        end = jpg_data.rfind(b'\xff\xd9')
        if end >= 0: jpg_data = jpg_data[:end+2]
        if len(jpg_data) > 500:
            b64 = 'data:image/jpeg;base64,' + base64.b64encode(jpg_data).decode()
            return None, b64
    
    # PNG tekshir
    png_start = rvf.find(b'\x89PNG\r\n\x1a\n')
    if png_start >= 0:
        png_data = rvf[png_start:]
        if len(png_data) > 500:
            b64 = 'data:image/png;base64,' + base64.b64encode(png_data).decode()
            return None, b64
    
    # EMF tekshir (formula rasmi)
    # Format: "Equation.3\r\nTMetafile\r\n" + 4byte_size + EMF_data
    tmet_pos = rvf.find(b'TMetafile\r\n')
    if tmet_pos >= 0:
        after = rvf[tmet_pos+11:]
        if len(after) >= 8:
            emf_size = struct.unpack_from('<I', after, 0)[0]
            emf_data = after[4:4+emf_size]
            if len(emf_data) >= emf_size and emf_size > 100:
                try:
                    tmpdir = tempfile.mkdtemp()
                    emf_path = os.path.join(tmpdir, 'f.emf')
                    png_path = os.path.join(tmpdir, 'f.png')
                    with open(emf_path, 'wb') as f:
                        f.write(emf_data)
                    r = subprocess.run(
                        ['libreoffice', '--headless', '--norestore',
                         '--convert-to', 'png', '--outdir', tmpdir, emf_path],
                        capture_output=True, timeout=30
                    )
                    if os.path.exists(png_path) and os.path.getsize(png_path) > 2000:
                        with open(png_path, 'rb') as f:
                            png_bytes = f.read()
                        b64 = 'data:image/png;base64,' + base64.b64encode(png_bytes).decode()
                        return None, b64
                except Exception as e:
                    print(f"EMF convert error: {e}", file=sys.stderr)
                finally:
                    for fp in [emf_path, png_path]:
                        try: os.unlink(fp)
                        except: pass
                    try: os.rmdir(tmpdir)
                    except: pass
    
    # UTF-16 matn o'qish (oddiy matnli RVF)
    lines = rvf.split(b'\r\n')
    if len(lines) >= 3:
        text_part = b'\r\n'.join(lines[2:])
        try:
            text = text_part.decode('utf-16-le', errors='replace').strip()
            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text).strip()
            if len(text) > 2:
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
    img_count = 0
    
    for i, block in enumerate(blocks):
        type_m = re.search(r'<QuestionTypeName>(.*?)</QuestionTypeName>', block)
        qtype = type_m.group(1).strip() if type_m else 'MultipleChoice'
        if qtype not in ('MultipleChoice', 'MultipleResponse'): continue
        
        # === SAVOL MATNINI O'QISH ===
        # Content bo'limi - faqat birinchi <Content>...</Content> (savol uchun)
        content_m = re.search(r'<Content>([\s\S]*?)</Content>', block)
        
        q_text = ''
        img_b64 = None
        
        if content_m:
            content = content_m.group(1)
            plain_m = re.search(r'<PlainText>([\s\S]*?)</PlainText>', content)
            plain = plain_m.group(1).strip() if plain_m else ''
            
            rvf_m = re.search(r'<RVFStoredPos>(\d+)</RVFStoredPos>\s*<RVFStoredLen>(\d+)</RVFStoredLen>', content)
            
            if rvf_m:
                rvf_pos = int(rvf_m.group(1))
                rvf_len = int(rvf_m.group(2))
                rvf_text, rvf_img = read_rvf(data, rvf_pos, rvf_len)
                
                if rvf_img:
                    q_text = plain
                    img_b64 = rvf_img
                    img_count += 1
                elif rvf_text and len(rvf_text) > len(plain):
                    q_text = rvf_text
                else:
                    q_text = plain
            else:
                q_text = plain
        
        if not q_text and not img_b64:
            continue
        
        # === JAVOBLARNI O'QISH ===
        # Answers_block - <Answer IsCorrect="..."> taglarini qidirish
        opts, corr = [], []
        
        # Barcha Answer taglarini topish
        for am in re.finditer(
            r'<Answer\s+IsCorrect="(Yes|No)"[\s\S]*?<Content>([\s\S]*?)</Content>',
            block):
            is_correct = am.group(1)
            ans_content = am.group(2)
            
            a_plain_m = re.search(r'<PlainText>([\s\S]*?)</PlainText>', ans_content)
            a_plain = a_plain_m.group(1).strip() if a_plain_m else ''
            
            a_rvf_m = re.search(r'<RVFStoredPos>(\d+)</RVFStoredPos>\s*<RVFStoredLen>(\d+)</RVFStoredLen>', ans_content)
            
            a_text = a_plain
            if a_rvf_m:
                a_pos = int(a_rvf_m.group(1))
                a_len = int(a_rvf_m.group(2))
                a_rvf_text, _ = read_rvf(data, a_pos, a_len)
                if a_rvf_text and len(a_rvf_text) > len(a_plain):
                    a_text = a_rvf_text
            
            if a_text:
                opts.append(a_text)
                if is_correct == 'Yes':
                    corr.append(len(opts)-1)
        
        if len(opts) >= 2 and corr:
            q = {
                'id': i, 'subject': 'math', 'topic': topic,
                'text': q_text or '(Rasm)',
                'options': opts,
                'correct': corr,
            }
            if img_b64:
                q['image'] = img_b64
            questions.append(q)
    
    print(f"Parsed: {len(questions)} questions, {img_count} images", file=sys.stderr)
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
