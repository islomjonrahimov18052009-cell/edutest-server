from flask import Flask, request, jsonify
from flask_cors import CORS
import subprocess, tempfile, os, struct, zlib, re, base64, json

app = Flask(__name__)
CORS(app)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

def find_xml_block(data):
    """Find and decompress the XML block from EasyQuizzy exe"""
    sig = b'\xef\xbb\xbf<?xml'
    for i in range(len(data)-6, max(0, len(data)-600000), -1):
        b0, b1 = data[i], data[i+1] if i+1 < len(data) else 0
        if b0 == 0x78 and b1 in (0x01, 0x9c, 0xda, 0x5e):
            chunk = data[i:i+600000]
            for method in [
                lambda c: zlib.decompress(c),
                lambda c: zlib.decompress(c[2:], -15),
                lambda c: zlib.decompress(c, -15),
            ]:
                try:
                    out = method(chunk)
                    if out.startswith(sig) or b'QuestionBlock' in out:
                        return out.decode('utf-8', errors='replace')
                except:
                    pass
    return None

def find_emf_blocks(data):
    """Find EMF image blocks in exe"""
    emf_magic = b'\x01\x00\x00\x00'
    emf_sigs = []
    i = 0
    while i < len(data) - 88:
        # EMF header: record type 1, record size >= 88, bounds
        if data[i:i+4] == b'\x01\x00\x00\x00':
            rec_size = struct.unpack_from('<I', data, i+4)[0] if i+4 < len(data)-4 else 0
            if 88 <= rec_size <= 1000:
                emf_sigs.append(i)
        i += 1
    return emf_sigs

def emf_to_png_base64(data, offset, max_size=500000):
    """Convert EMF bytes to PNG base64"""
    try:
        emf_data = data[offset:offset+max_size]
        # Find end of EMF (EOF record type = 14)
        for j in range(0, len(emf_data)-8, 4):
            rec_type = struct.unpack_from('<I', emf_data, j)[0]
            if rec_type == 14:  # EMF EOF record
                rec_size = struct.unpack_from('<I', emf_data, j+4)[0]
                emf_data = emf_data[:j+rec_size]
                break
        
        with tempfile.NamedTemporaryFile(suffix='.emf', delete=False) as f:
            f.write(emf_data)
            emf_path = f.name
        
        png_path = emf_path.replace('.emf', '.png')
        result = subprocess.run(
            ['libreoffice', '--headless', '--convert-to', 'png', '--outdir', 
             os.path.dirname(emf_path), emf_path],
            capture_output=True, timeout=30
        )
        
        if os.path.exists(png_path):
            with open(png_path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode()
            os.unlink(emf_path)
            os.unlink(png_path)
            return b64
        
        os.unlink(emf_path)
        return None
    except Exception as e:
        return None

def parse_xml(text):
    """Parse EasyQuizzy XML"""
    name_m = re.search(r'<GlobalSettings[\s\S]*?<Name>([\s\S]*?)</Name>', text)
    topic = name_m.group(1).strip() if name_m else 'Test'
    
    qta_m = re.search(r'<QuestionsToAsk>(\d+)</QuestionsToAsk>', text)
    questions_to_ask = int(qta_m.group(1)) if qta_m else 20
    
    blocks = re.findall(r'<QuestionBlock[^>]*>([\s\S]*?)</QuestionBlock>', text)
    questions = []
    
    for i, block in enumerate(blocks):
        type_m = re.search(r'<QuestionTypeName>(.*?)</QuestionTypeName>', block)
        qtype = type_m.group(1).strip() if type_m else 'MultipleChoice'
        if qtype not in ('MultipleChoice', 'MultipleResponse'):
            continue
        
        c_m = re.search(r'<Content>\s*<PlainText>([\s\S]*?)</PlainText>', block)
        if not c_m or not c_m.group(1).strip():
            continue
        
        q_text = c_m.group(1).strip().replace('\r\n', ' ').replace('\n', ' ')
        opts, corr = [], []
        
        for am in re.finditer(r'<Answer\s+IsCorrect="(Yes|No)"[\s\S]*?<Content>\s*<PlainText>([\s\S]*?)</PlainText>', block):
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
                'text': q_text,
                'options': opts,
                'correct': corr,
                'isMulti': qtype == 'MultipleResponse',
                'questionsToAsk': questions_to_ask
            })
    
    return topic, questions_to_ask, questions

@app.route('/parse', methods=['POST'])
def parse_exe():
    try:
        data = request.get_data()
        if not data:
            return jsonify({'error': 'Fayl bo\'sh'}), 400
        
        # Find XML
        xml_text = find_xml_block(data)
        if not xml_text:
            return jsonify({'error': 'EasyQuizzy fayli tanilmadi'}), 400
        
        topic, questions_to_ask, questions = parse_xml(xml_text)
        
        if not questions:
            return jsonify({'error': 'Savollar topilmadi'}), 400
        
        # Try to find images - match EMF blocks with questions
        # Look for image references in XML
        img_blocks = re.findall(r'<QuestionBlock[^>]*>[\s\S]*?<ImageRef>([\s\S]*?)</ImageRef>[\s\S]*?</QuestionBlock>', xml_text)
        
        # Try LibreOffice conversion if available
        try:
            subprocess.run(['which', 'libreoffice'], capture_output=True, check=True, timeout=5)
            has_lo = True
        except:
            has_lo = False
        
        if has_lo:
            # Find EMF offsets in binary
            emf_offsets = []
            i = 0
            while i < len(data) - 88:
                if (data[i] == 0x01 and data[i+1] == 0x00 and data[i+2] == 0x00 and data[i+3] == 0x00):
                    rec_size = struct.unpack_from('<I', data, i+4)[0] if i+4 < len(data)-4 else 0
                    if 88 <= rec_size <= 200:
                        emf_offsets.append(i)
                i += 1
            
            # Match images to questions that have image placeholders
            img_idx = 0
            for q in questions:
                if img_idx < len(emf_offsets):
                    # Check if question block has image
                    b64 = emf_to_png_base64(data, emf_offsets[img_idx])
                    if b64:
                        q['img'] = b64
                        img_idx += 1
        
        return jsonify({
            'topic': topic,
            'questionsToAsk': questions_to_ask,
            'questions': questions
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
