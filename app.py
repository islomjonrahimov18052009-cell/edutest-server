from flask import Flask, request, jsonify
import zlib, re, base64, os, subprocess, tempfile, json

app = Flask(__name__)

def extract_emf_as_base64(emf_bytes):
    with tempfile.NamedTemporaryFile(suffix='.emf', delete=False, dir='/tmp') as f:
        f.write(emf_bytes)
        emf_path = f.name
    try:
        subprocess.run(
            ['libreoffice','--headless','--convert-to','png',emf_path,'--outdir','/tmp/'],
            capture_output=True, timeout=30
        )
        png_path = emf_path.replace('.emf', '.png')
        if os.path.exists(png_path):
            with open(png_path,'rb') as f:
                data = f.read()
            os.unlink(png_path)
            return base64.b64encode(data).decode()
    except Exception as e:
        print(f"EMF error: {e}")
    finally:
        if os.path.exists(emf_path):
            os.unlink(emf_path)
    return None

def parse_exe(file_bytes):
    data = file_bytes
    xml_text = None
    for i in range(len(data)-2, 0, -1):
        if data[i] == 0x78 and data[i+1] in (0x01, 0x9c, 0xda, 0x5e):
            try:
                out = zlib.decompress(data[i:])
                text = out.decode('utf-8-sig', errors='replace')
                if 'QuestionBlock' in text and 'Answer' in text:
                    xml_text = text
                    break
            except: pass
    
    if not xml_text:
        return {'error': 'EasyQuizzy fayli tanilmadi'}
    
    name_m = re.search(r'<GlobalSettings[\s\S]*?<Name>([\s\S]*?)</Name>', xml_text)
    topic = name_m.group(1).strip() if name_m else 'Test'
    qta_m = re.search(r'<QuestionsToAsk>(\d+)</QuestionsToAsk>', xml_text)
    qta = int(qta_m.group(1)) if qta_m else 20
    
    blocks = re.findall(r'<QuestionBlock[^>]*>([\s\S]*?)</QuestionBlock>', xml_text)
    questions = []
    
    for idx, block in enumerate(blocks):
        type_m = re.search(r'<QuestionTypeName>(.*?)</QuestionTypeName>', block)
        qtype = type_m.group(1).strip() if type_m else 'MultipleChoice'
        if qtype not in ('MultipleChoice','MultipleResponse'): continue
        
        plain_m = re.search(r'<Content>\s*<PlainText>([\s\S]*?)</PlainText>', block)
        qtext = plain_m.group(1).strip().replace('\r\n',' ').replace('\n',' ') if plain_m else ''
        if not qtext: continue
        
        rvf_pos_m = re.search(r'<RVFStoredPos>(\d+)</RVFStoredPos>', block)
        rvf_len_m = re.search(r'<RVFStoredLen>(\d+)</RVFStoredLen>', block)
        img_b64 = None
        
        if rvf_pos_m and rvf_len_m:
            rpos = int(rvf_pos_m.group(1))
            rlen = int(rvf_len_m.group(1))
            if rlen > len(qtext.encode('utf-8')) + 500:
                chunk = data[rpos:rpos+rlen]
                emf_off = chunk.find(b'\x01\x00\x00\x00')
                if emf_off >= 0:
                    img_b64 = extract_emf_as_base64(chunk[emf_off:])
        
        opts, corr = [], []
        ans_re = re.findall(
            r'<Answer\s+IsCorrect="(Yes|No)"[^>]*>[\s\S]*?<Content>\s*<PlainText>([\s\S]*?)</PlainText>', block)
        for is_correct, atext in ans_re:
            atext = atext.strip()
            if atext:
                opts.append(atext)
                if is_correct == 'Yes': corr.append(len(opts)-1)
        
        if len(opts) >= 2 and corr:
            q = {
                'id': idx, 'subject': 'math', 'topic': topic,
                'text': qtext, 'options': opts, 'correct': corr,
                'isMulti': qtype == 'MultipleResponse',
                'questionsToAsk': qta
            }
            if img_b64: q['img'] = img_b64
            questions.append(q)
    
    return {'questions': questions, 'topic': topic, 'questionsToAsk': qta}

@app.route('/')
def index():
    return jsonify({'status': 'EduTest Pro Server ishlayapti!'})

@app.route('/parse', methods=['POST', 'OPTIONS'])
def parse():
    if request.method == 'OPTIONS':
        response = jsonify({})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response
    
    try:
        file_bytes = request.get_data()
        if not file_bytes:
            return jsonify({'error': 'Fayl yuklanmadi'}), 400
        
        result = parse_exe(file_bytes)
        response = jsonify(result)
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response
    except Exception as e:
        response = jsonify({'error': str(e)})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response, 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
