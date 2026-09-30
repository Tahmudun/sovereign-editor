"""Rebuild the checked-in Thumb object image; development only, requires clang/pyelftools.

Internal addresses are ABS32 relocations applied by scene_runtime at placement.
Native calls use explicit pinned addresses. ELF unwind sections are not loaded.
"""
import hashlib,json,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'work/tiana-events-1/python-tools'))
from elftools.elf.elffile import ELFFile
src=ROOT/'sovereign_editor/native/scene_collect.c'
with tempfile.TemporaryDirectory() as temp:
    obj=Path(temp)/'scene_collect.o'
    subprocess.run(['clang','--target=armv5te-none-eabi','-mthumb','-Oz','-ffreestanding',
                    '-fno-builtin','-fno-unwind-tables','-fno-asynchronous-unwind-tables',
                    '-c',str(src),'-o',str(obj)],check=True)
    with obj.open('rb') as f:
        elf=ELFFile(f);data=bytearray();offsets={}
        for i,s in enumerate(elf.iter_sections()):
            if s.name in ('.text','.rodata'):
                data.extend(b'\0'*(-len(data)%4));offsets[i]=len(data);data.extend(s.data())
        symbols=elf.get_section_by_name('.symtab');relocations=[]
        for s in elf.iter_sections():
            if s['sh_type']!='SHT_REL' or s['sh_info'] not in offsets:continue
            for r in s.iter_relocations():
                assert r['r_info_type']==2, (s.name,r) # R_ARM_ABS32
                sym=symbols.get_symbol(r['r_info_sym'])
                assert sym['st_shndx'] in offsets, sym.name
                relocations.append([offsets[s['sh_info']]+r['r_offset'],offsets[sym['st_shndx']]+sym['st_value']])
        names={s.name:offsets[s['st_shndx']]+s['st_value'] for s in symbols.iter_symbols()
               if s.name.startswith('collect_') and s['st_shndx'] in offsets}
    out={'source_sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'code':data.hex(),
         'relocations':relocations,'symbols':names}
    target=ROOT/'sovereign_editor/native/scene_collect.json'
    target.write_text(json.dumps(out,indent=2)+'\n')
    print(json.dumps({'bytes':len(data),'relocations':len(relocations),'symbols':names}))
