from flask import Flask, request, jsonify
from flask_cors import CORS
import subprocess, tempfile, os, struct, zlib, re, base64, json, sys

app = Flask(__name__)
CORS(app)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

def find_xml(data):
    bom_xml = b'\xef\xbb\xbf<?xml'
    plain_xml = b'<?xml'
    
    # Search backwards for zlib blocks
    for i in range(len(data)-6, max(0, len(data)-2000000), -1):
        b0 = data[i]
        if i+1 >= len(data):
            continue
        b1 = data[i+1]
        if b0 == 0x78 and b1 in (0x01, 0x9c, 0xda, 0x5e):
            try:
                out = zlib.decompress(data[i:i+1000000])
                if b'QuestionBlock' in out or bom_xml in out or plain_xml in out:
                    return out.decode('utf-8', errors='replace')
            except:
                pass
            try:
                out = zlib.decompress(data[i:i+1000000], 47)
                if b'QuestionBlock' in out or bom_xml in out or plain_xml in out:
                    return out.decode('utf-8', errors='replace')
            except:
                pass
            try:
                out = zlib.decompress(data[i+2:i+1000000], -15)
                if b'QuestionBlock' in out or bom_xml in out or plain_xml in out:
                    return out.decode('utf-8', errors='replace')
            except:
                pass
    return None

def emf_to_png(emf_bytes):
    try:
        with tempfile.NamedTemporaryFile(suffix='.emf', delete=False, dir='/tmp') as f:
            f.write(emf_bytes)
            emf_path = f.name
        
        result = subprocess.run(
            ['libreoffice', '--headless', '--convert-to', 'png', '--outdir', '/tmp', emf_path],
            capture_output=True, timeout=30, text=True
        )
        print(f"LibreOffice stdout: {result.stdout}", file=sys.stderr)
        print(f"LibreOffice stderr: {result.stderr}", file=sys.stderr)
        
        png_path = emf_path.replace('.emf', '.png')
        if os.path.exists(png_path):
            with open(png_path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode()
            os.unlink(png_path)
            os.unlink(emf_path)
            print(f"EMF converted OK, size={len(b64)}", file=sys.stderr)
            return b64
        else:
            print(f"PNG not found at {png_path}", file=sys.stderr)
            if os.path.exists(emf_path):
                os.unlink(emf_path)
            return None
    except Exception as e:
        print(f"EMF conversion error: {e}", file=sys.stderr)
        return None

def find_emf_images(data):
    images = []
    i = 0
    while i < len(data) - 100:
        if (data[i] == 0x01 and data[i+1] == 0x00 and
            data[i+2] == 0x00 and data[i+3] == 0x00):
            try:
                rec_size = struct.unpack_from('<I', data, i+4)[0]
                if 88 <= rec_size <= 200:
                    # Find EMF EOF record (type 14)
                    end = -1
                    for j in range(i + rec_size, min(i + 500000, len(data) - 8), 4):
                        try:
                            rt = struct.unpack_from('<I', data, j)[0]
                            rs = struct.unpack_from('<I', data, j+4)[0]
                            if rt == 14 and 16 <= rs <= 100:
                                end = j + rs
                                break
                        except:
                            break
                    
                    if end > i + rec_size:
                        emf_bytes = data[i:end]
                        if len(emf_bytes) > 200:
                            images.append(emf_bytes)
                            i = end
                            continue
            except:
                pass
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
        if qtype not in ('MultipleChoice', 'MultipleResponse'):
            continue

        c_m = re.search(r'<Content>\s*<PlainText>([\s\S]*?)</PlainText>', block)
        q_text = c_m.group(1).strip() if c_m else ''
        
        has_image = bool(re.search(r'<ImageRef|<Image>|<HasImage>true', block, re.IGNORECASE))

        opts, corr = [], []
        for am in re.finditer(
            r'<Answer\s+IsCorrect="(Yes|No)"[\s\S]*?<Content>\s*<PlainText>([\s\S]*?)</PlainText>',
            block):
            a_text = am.group(2).strip()
            if a_text:
                opts.append(a_text)
                if am.group(1) == 'Yes':
                    corr.append(len(opts)-1)

        if len(opts) >= 2 and corr:
            questions.append({
                'id': i,
                'subject': 'math',
                'topic': topic,
                'text': q_text or '(Rasmga qarang)',
                'options': opts,
                'correct': corr,
                'isMulti': qtype == 'MultipleResponse',
                'hasImage': has_image,
                'questionsToAsk': questions_to_ask
            })

    return topic, questions_to_ask, questions

@app.route('/parse', methods=['POST'])
def parse_exe():
    try:
        data = request.get_data()
        print(f"Received file: {len(data)} bytes", file=sys.stderr)
        
        if not data or len(data) < 100:
            return jsonify({'error': 'Fayl bo\'sh'}), 400

        xml_text = find_xml(data)
        if not xml_text:
            return jsonify({'error': 'EasyQuizzy fayli tanilmadi'}), 400

        print(f"XML found, length={len(xml_text)}", file=sys.stderr)
        
        topic, questions_to_ask, questions = parse_questions(xml_text)
        print(f"Questions parsed: {len(questions)}", file=sys.stderr)
        
        if not questions:
            return jsonify({'error': 'Savollar topilmadi'}), 400

        # Find EMF images
        emf_images = find_emf_images(data)
        print(f"EMF images found: {len(emf_images)}", file=sys.stderr)

        if emf_images:
            png_images = []
            for emf_bytes in emf_images:
                b64 = emf_to_png(emf_bytes)
                if b64:
                    png_images.append(b64)
            
            print(f"PNG images converted: {len(png_images)}", file=sys.stderr)
            
            img_idx = 0
            for q in questions:
                if q.get('hasImage') and img_idx < len(png_images):
                    q['img'] = png_images[img_idx]
                    img_idx += 1
            
            if img_idx == 0 and png_images:
                img_idx = 0
                for q in questions:
                    if img_idx < len(png_images):
                        q['img'] = png_images[img_idx]
                        img_idx += 1

        return jsonify({
            'topic': topic,
            'questionsToAsk': questions_to_ask,
            'questions': questions,
            'imageCount': len(emf_images)
        })

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
