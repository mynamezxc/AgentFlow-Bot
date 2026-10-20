"""Owner uploads: immutable bytes, bounded text extraction and actual image input."""
import base64
import binascii
import hashlib
import io
import json
import mimetypes
import struct
import subprocess
import sys
import threading
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .store import DomainError, uid

IMAGES = {".png":"image/png", ".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".webp":"image/webp"}
TEXT = {".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".toml", ".html", ".css", ".js", ".ts", ".tsx", ".jsx", ".py", ".sql", ".log", ".svg"}
MAX_IMAGE = 5*1024*1024
MAX_FILE = 4*1024*1024
MAX_TEXT = 120000


class Attachments:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.root = self.store.directory / "attachments"
        self.root.mkdir(exist_ok=True)
        self.install_lock = threading.Lock()
        self.packages = self.store.directory / "parser-packages"
        if self.packages.is_dir(): sys.path.insert(0,str(self.packages))

    def pdf_text(self, raw):
        with self.install_lock:
            try:
                from pypdf import PdfReader
            except ImportError:
                self.packages.mkdir(exist_ok=True)
                try:
                    result=subprocess.run([sys.executable,"-m","pip","install","--disable-pip-version-check","--no-input",
                        "--target",str(self.packages),"pypdf==6.21.1"], capture_output=True, timeout=180,
                        env=self.engine.providers.child_environment(), creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                    if result.returncode: raise OSError()
                    sys.path.insert(0,str(self.packages))
                    from pypdf import PdfReader
                except (OSError,ImportError,subprocess.TimeoutExpired):
                    raise DomainError("Could not install the PDF reader. Check network access and pip, then upload again or send page images.",502) from None
        reader=PdfReader(io.BytesIO(raw))
        if reader.is_encrypted: raise DomainError("This PDF is locked. Send an unlocked copy or page images.")
        if len(reader.pages)>100: raise DomainError("This PDF exceeds 100 pages. Split the relevant section for accurate reading.")
        parts=[]; has_text=False
        for index,page in enumerate(reader.pages):
            text=page.extract_text() or ""
            has_text=has_text or bool(text.strip())
            if len("".join(parts))+len(text)>MAX_TEXT: raise DomainError("This PDF is too long. Split the relevant section.")
            parts.append("[Trang "+str(index+1)+"]\n"+text)
        if not has_text:
            raise DomainError("This scanned PDF has no text layer. Send page images for direct visual inspection.")
        return "\n\n".join(parts), "Text extracted by page. PDF images and charts are not included. Send page images for visual or layout analysis."

    def office_text(self, raw, suffix):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries=archive.infolist()
            if len(entries)>1000 or sum(i.file_size for i in entries)>24*1024*1024:
                raise DomainError("The expanded document content exceeds the size limit.")
            parts=[]
            for info in sorted(entries,key=lambda info:info.filename!="xl/sharedStrings.xml"):
                name=info.filename
                selected=(suffix==".docx" and name=="word/document.xml" or suffix==".pptx" and name.startswith("ppt/slides/slide") and name.endswith(".xml")
                    or suffix==".xlsx" and (name=="xl/sharedStrings.xml" or name.startswith("xl/worksheets/sheet") and name.endswith(".xml")))
                if not selected: continue
                tree=ET.fromstring(archive.read(info))
                if suffix==".xlsx" and name!="xl/sharedStrings.xml":
                    shared=parts[0].get("shared",[]) if parts and isinstance(parts[0],dict) else []
                    lines=[]
                    for row in tree.iter():
                        if row.tag.rsplit('}',1)[-1]!="row": continue
                        cells=[]
                        for cell in row:
                            values=[x.text or "" for x in cell.iter() if x.tag.rsplit('}',1)[-1] in ("v","t","f")]
                            if cell.attrib.get("t")=="s" and values:
                                values=[shared[int(values[0])]] if values[0].isdigit() and int(values[0])<len(shared) else values
                            cells.append(cell.attrib.get("r","")+": "+" | ".join(values))
                        lines.append("\t".join(cells))
                    parts.append(name+"\n"+"\n".join(lines))
                elif suffix==".xlsx":
                    values=["".join(x.text or "" for x in item.iter() if x.tag.rsplit('}',1)[-1]=="t") for item in tree if item.tag.rsplit('}',1)[-1]=="si"]
                    parts.insert(0,{"shared":values})
                else:
                    parts.append(name+"\n"+"\n".join(x.text for x in tree.iter() if x.tag.rsplit('}',1)[-1]=="t" and x.text))
            return "\n\n".join(p for p in parts if isinstance(p,str)), "Text content only. Macros and formulas are not executed; embedded images are not included."

    def upload(self, data):
        name=str(data.get("name",""))
        if not name or len(name)>180 or any(c in name for c in ('/','\\','\x00','\r','\n')) or name.startswith('.'):
            raise DomainError("Invalid filename.")
        suffix=Path(name).suffix.casefold()
        if suffix not in set(IMAGES)|TEXT|{".pdf",".docx",".xlsx",".pptx"}:
            raise DomainError("Supported formats: PNG/JPG/WebP, text/code, text PDFs, DOCX, XLSX and PPTX.",415)
        try: raw=base64.b64decode(data.get("data",""),validate=True)
        except (ValueError,TypeError): raise DomainError("Invalid file data.") from None
        if not raw or len(raw)>(MAX_IMAGE if suffix in IMAGES else MAX_FILE):
            raise DomainError("Images may be up to 5 MB; documents up to 4 MB per file.",413)
        text,notice="",""
        try:
            if suffix in IMAGES:
                if suffix==".png":
                    if not raw.startswith(b'\x89PNG\r\n\x1a\n') or len(raw)<45 or raw[12:16]!=b'IHDR': raise ValueError()
                    w,h=struct.unpack('>II',raw[16:24])
                    if not 0<w<=12000 or not 0<h<=12000 or w*h>40000000: raise ValueError()
                    offset=8; has_data=False; ended=False
                    while offset+12<=len(raw):
                        size=struct.unpack('>I',raw[offset:offset+4])[0];kind=raw[offset+4:offset+8];end=offset+12+size
                        if end>len(raw): raise ValueError()
                        if binascii.crc32(raw[offset+4:end-4])&0xffffffff != struct.unpack('>I',raw[end-4:end])[0]:raise ValueError()
                        has_data=has_data or kind==b'IDAT'
                        offset=end
                        if kind==b'IEND':ended=size==0 and offset==len(raw);break
                    if not ended or not has_data:raise ValueError()
                elif suffix in (".jpg",".jpeg") and not (raw.startswith(b'\xff\xd8\xff') and raw.endswith(b'\xff\xd9')): raise ValueError()
                elif suffix==".webp" and not (raw.startswith(b'RIFF') and raw[8:12]==b'WEBP' and struct.unpack('<I',raw[4:8])[0]+8==len(raw)): raise ValueError()
                notice="The original image is sent to an image-capable model."
            elif suffix==".pdf":
                if not raw.startswith(b'%PDF-'): raise ValueError()
                text,notice=self.pdf_text(raw)
            elif suffix in (".docx",".xlsx",".pptx"):
                text,notice=self.office_text(raw,suffix)
            else:
                text=raw.decode("utf-16" if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else "utf-8-sig")
                if '\x00' in text: raise ValueError()
            if suffix not in IMAGES and (not text.strip() or len(text)>MAX_TEXT):
                raise DomainError("The file has no readable text or exceeds 120,000 characters. Send a smaller section.")
        except DomainError: raise
        except Exception: raise DomainError("Cannot read this file format. Check the original and retry. Invalid content was not sent to the model.") from None
        attachment_id=uid("upload")
        path=self.root/(attachment_id+suffix)
        path.write_bytes(raw)
        return self.store.create("attachments",{"id":attachment_id,"name":name,"suffix":suffix,
            "mime":IMAGES.get(suffix,mimetypes.guess_type(name)[0] or "application/octet-stream"),
            "bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest(),"text":self.engine.vault.redact(text),
            "notice":notice,"is_image":suffix in IMAGES,"project_id":None})

    def public(self, item):
        return {k:v for k,v in item.items() if k!="text"}

    def path(self, item):
        path=self.root/(item["id"]+item["suffix"])
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=item["sha256"]:
            raise DomainError("The attachment changed or is no longer available locally.",409)
        return path

    def bind(self, ids, project_id):
        if not isinstance(ids,list) or any(not isinstance(i,str) for i in ids) or len(ids)>6 or len(set(ids))!=len(ids):
            raise DomainError("A message can contain up to 6 distinct attachments.")
        items=[self.store.get("attachments",i) for i in ids]
        if sum(len(i.get("text","")) for i in items)>240000:
            raise DomainError("Documents exceed 240,000 characters. Send smaller sections.")
        for item in items:
            if item.get("project_id") not in (None,project_id): raise DomainError("This attachment belongs to another conversation or project. Select the file again.",403)
            self.path(item)
        for item in items:
            if not item.get("project_id"): self.store.patch("attachments",item["id"],{"project_id":project_id})

    def for_run(self, run):
        items=[self.store.get("attachments",i) for i in run.get("attachment_ids",[])]
        if any(i.get("project_id")!=run.get("project_id") for i in items): raise DomainError("This file does not belong to the current task.",403)
        return items

    def context(self, run):
        return [{**self.public(i),"content":i.get("text","")} for i in self.for_run(run)]

    def images(self, run):
        return [{"path":str(self.path(i)),"mime":i["mime"],"name":i["name"]} for i in self.for_run(run) if i["is_image"]]
