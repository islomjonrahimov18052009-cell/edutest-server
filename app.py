from flask import Flask, request, jsonify
from flask_cors import CORS
import subprocess, tempfile, os, struct, zlib, re, base64, sys

app = Flask(__name__)
CORS(app)

@app.route('/health', methods=['GET'])
def health():
    # Check available tools
    tools = {}
    for tool in ['libreoffice', 'convert', 'inkscape']:
        try:
            r = subprocess.run(['which', tool], capture_output=True, text=True)
            tools[tool] = r.stdout.strip()
        except:
            tools[tool] = 'not found'
    return jsonify({'status': 'ok', 'tools': tools})

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

def emf_to_png_imagemagick(emf_bytes):
    """Try ImageMagick convert"""
    try:
        with tempfile.NamedTemporaryFile(suffix='.emf', delete=False, dir='/tmp') as f:
            f.write(emf_bytes)
            emf_path = f.name
        png_path = emf_path.replace('.emf', '.png')
        
        r = subprocess.run(
            ['convert', emf_path, png_path],
            capture_output=True, timeout=15
        )
        print(f"ImageMagick: {r.returncode}, stderr: {r.stderr[:200]}", file=sys.stderr)
        
        if os.path.exists(png_path):
            with open(png_path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode()
            os.unlink(png_path)
            os.unlink(emf_path)
            return b64
        if os.path.exists(emf_path): os.unlink(emf_path)
        return None
    except Exception as e:
        print(f"ImageMagick error: {e}", file=sys.stderr)
        return None

def emf_to_png_libreoffice(emf_bytes):
    """Try LibreOffice with --infilter"""
    try:
        with tempfile.NamedTemporaryFile(suffix='.emf', delete=False, dir='/tmp') as f:
            f.write(emf_bytes)
            emf_path = f.name
        png_path = emf_path.replace('.emf', '.png')
        
        r = subprocess.run(
            ['libreoffice', '--headless', '--norestore',
             '--infilter=EMF', '--convert-to', 'png',
             '--outdir', '/tmp', emf_path],
            capture_output=True, timeout=60
        )
        print(f"LibreOffice: rc={r.returncode}, stdout={r.stdout[:100]}, stderr={r.stderr[:200]}", file=sys.stderr)
        
        if os.path.exists(png_path):
            with open(png_path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode()
            os.unlink(png_path)
            os.unlink(emf_path)
            return b64
        if os.path.exists(emf_path): os.unlink(emf_path)
        return None
    except Exception as e:
        print(f"LibreOffice error: {e}", file=sys.stderr)
        return None

def emf_to_png(emf_bytes):
    # Try ImageMagick first (faster)
    b64 = emf_to_png_imagemagick(emf_bytes)
    if b64: return b64
    # Fallback to LibreOffice
    b64 = emf_to_png_libreoffice(emf_bytes)
    return b64

def find_emf_images(data):
    images = []
    i = 0
    while i < len(data) - 100:
        if (data[i] == 0x01 and data[i+1] == 0x00 and
            data[i+2] == 0x00 and data[i+3] == 0x00):
            try:
                rec_size = struct.unpack_from('<I', data, i+4)[0]
                if 88 <= rec_size <= 200:
                    end = -1
                    for j in range(i + rec_size, min(i + 500000, len(data) - 8), 4):
                        try:
                            rt = struct.unpack_from('<I', data, j)[0]
                            rs = struct.unpack_from('<I', data, j+4)[0]
                            if rt == 14 and 16 <= rs <= 100:
                                end = j + rs
                                break
                        except: break
                    if end > i + rec_size:
                        emf_bytes = data[i:end]
                        if len(emf_bytes) > 200:
                            images.append(emf_bytes)
                            i = end
                            continue
            except: pass
        i += 1
    return images

def parse_questions(xml_text):
    name_m = re.search(r'<Name>([\s\S]*?)</Name>', xml_text)
    topic = name_m.group(1).strip() if name_m else 'Test'
    qta_m = re.search(r'<QuestionsToAsk>(\d+)</QuestionsToAsk>', xml_text)
    questions_to_ask = int(qta_m.group(1)) if qta_m else 20
    blocks = re.findall(r'<QuestionBlock[^>]*>([\s\S]*?)</QuestionBlock>', xml_text)
    questions = []
    for i, block in enumerate(blocks):
        type_m = re.search(r'<QuestionTypeName>(.*?)</QuestionTypeName>', block)
        qtype = type_m.group(1).strip() if type_m else 'MultipleChoice'
        if qtype not in ('MultipleChoice', 'MultipleResponse'): continue
        c_m = re.search(r'<Content>\s*<PlainText>([\s\S]*?)</PlainText>', block)
        q_text = c_m.group(1).strip() if c_m else ''
        has_image = bool(re.search(r'<ImageRef|<Image>|<HasImage>true', block, re.IGNORECASE))
        opts, corr = [], []
        for am in re.finditer(r'<Answer\s+IsCorrect="(Yes|No)"[\s\S]*?<Content>\s*<PlainText>([\s\S]*?)</PlainText>', block):
            a_text = am.group(2).strip()
            if a_text:
                opts.append(a_text)
                if am.group(1) == 'Yes': corr.append(len(opts)-1)
        if len(opts) >= 2 and corr:
            questions.append({
                'id': i, 'subject': 'math', 'topic': topic,
                'text': q_text or '(Rasmga qarang)', 'options': opts,
                'correct': corr, 'isMulti': qtype == 'MultipleResponse',
                'hasImage': has_image, 'questionsToAsk': questions_to_ask
            })
    return topic, questions_to_ask, questions

@app.route('/parse', methods=['POST'])
def parse_exe():
    try:
        data = request.get_data()
        print(f"File received: {len(data)} bytes", file=sys.stderr)
        if not data or len(data) < 100:
            return jsonify({'error': 'Fayl bo\'sh'}), 400

        xml_text = find_xml(data)
        if not xml_text:
            return jsonify({'error': 'EasyQuizzy fayli tanilmadi'}), 400

        topic, questions_to_ask, questions = parse_questions(xml_text)
        print(f"Questions: {len(questions)}, topic: {topic}", file=sys.stderr)
        if not questions:
            return jsonify({'error': 'Savollar topilmadi'}), 400

        emf_images = find_emf_images(data)
        print(f"EMF found: {len(emf_images)}", file=sys.stderr)

        if emf_images:
            png_images = []
            for emf_bytes in emf_images:
                b64 = emf_to_png(emf_bytes)
                if b64:
                    png_images.append(b64)
            
            print(f"PNG converted: {len(png_images)}/{len(emf_images)}", file=sys.stderr)
            
            img_idx = 0
            for q in questions:
                if q.get('hasImage') and img_idx < len(png_images):
                    q['img'] = png_images[img_idx]; img_idx += 1
            if img_idx == 0 and png_images:
                for q in questions:
                    if img_idx < len(png_images):
                        q['img'] = png_images[img_idx]; img_idx += 1

        return jsonify({'topic': topic, 'questionsToAsk': questions_to_ask,
                       'questions': questions, 'imageCount': len(emf_images)})
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
