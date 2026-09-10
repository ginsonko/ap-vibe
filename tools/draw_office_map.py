"""Build the original office bitmap locally; no model request or runtime dependency."""
from pathlib import Path
import hashlib
import json
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'apps/studio/public/office-map.png'
im = Image.new('RGB', (480, 320), '#edf5f1')
d = ImageDraw.Draw(im)


def rect(box, color, outline=None):
    d.rectangle(tuple(int(x) for x in box), fill=color, outline=outline)


def plant(x, y):
    rect((x-4,y+3,x+4,y+9),'#a97963')
    rect((x-5,y+2,x+5,y+4),'#cf9d7c')
    rect((x-1,y-10,x+1,y+2),'#377768')
    for dx,dy in [(-6,-9),(2,-13),(-5,-4),(2,-7)]:
        rect((x+dx,y+dy,x+dx+5,y+dy+4),'#64a487')
        rect((x+dx,y+dy,x+dx+2,y+dy+1),'#a2d1a0')


def monitor(x,y,color):
    rect((x-9,y-9,x+10,y+3),'#546c75')
    rect((x-7,y-7,x+8,y+1),color)
    rect((x-5,y-5,x+4,y-4),'#eef9fb')
    rect((x-5,y-2,x+1,y-1),'#c3e8f0')
    rect((x-1,y+4,x+2,y+6),'#637981')
    rect((x-5,y+7,x+6,y+8),'#92a7ac')
    rect((x-10,y+11,x+8,y+14),'#c4d5d3','#97b0af')


def desk(x,y,color,screen=True):
    rect((x-20,y+6,x+22,y+13),'#abc1be')
    rect((x-23,y-2,x+24,y+9),'#7f9997')
    rect((x-23,y-5,x+24,y+5),'#f7fbf8','#afc6bc')
    rect((x-20,y+10,x-17,y+15),'#617c79')
    rect((x+18,y+10,x+21,y+15),'#617c79')
    if screen:monitor(x,y-7,color)
    rect((x-8,y+21,x+9,y+26),'#718f91')
    rect((x-10,y+15,x+11,y+22),color,'#7b9c94')
    rect((x-6,y+16,x+7,y+17),'#d9edf1')


floors=['#d9ece4','#e2edf4','#e8e5f0','#f2e4e9','#f3efdc','#deece4']
for i,color in enumerate(floors):
    x=(i%3)*160;y=0 if i<3 else 170
    rect((x+2,y+1,x+157,y+149),color)
    for yy in range(y+16,y+150,16):
        d.line((x+4,yy,x+157,yy), fill='#cdddda' if i%3==0 else '#d6dfe3')
    for xx in range(x+8,x+157,16):
        d.line((xx,y+2,xx,y+149),fill='#f4f7f3')
    rect((x,y,x+159,y+19),'#91aaa6')
    rect((x+3,y+2,x+156,y+16),'#edf5ee')
    rect((x+3,y+17,x+156,y+19),'#b9cec4')
    rect((x,y+20,x+2,y+150),'#8ba7a0')
    rect((x+158,y+20,x+159,y+150),'#8ba7a0')
    # Window with a pixel skyline, not an atmospheric light effect.
    rect((x+60,y+3,x+101,y+15),'#8fb8c2','#9dbbb4')
    rect((x+62,y+4,x+99,y+13),'#c8e9ed')
    for xx,hh in [(65,4),(73,6),(84,3),(93,5)]:rect((x+xx,y+14-hh,x+xx+4,y+14),'#a1c9c6')
    rect((x+80,y+3,x+81,y+15),'#eaf7ef')
    plant(x+16,y+43);plant(x+145,y+40)
    if i==0:
        rect((x+26,y+23,x+64,y+45),'#93bcae','#607f78')
        for dx,dy,col in [(30,26,'#fff0af'),(43,28,'#d5eaff'),(54,26,'#f1c0bf'),(34,37,'#fff')]:rect((x+dx,y+dy,x+dx+7,y+dy+5),col)
        desk(x+107,y+37,'#95b9aa')
    elif i==1:
        for sx in (30,92):
            rect((x+sx,y+22,x+sx+37,y+47),'#acbec6','#7495a0')
            for sy in (24,35):
                for j,c in enumerate(['#78a7c2','#b9c690','#d399a0','#ccb879','#7fa8a2']):
                    rect((x+sx+3+j*6,y+sy,x+sx+6+j*6,y+sy+8-j%2),c)
                rect((x+sx,y+sy+9,x+sx+37,y+sy+10),'#688da1')
    elif i in (2,3):
        desk(x+51,y+35,'#b5a8cb' if i==2 else '#d5a5b4')
        desk(x+112,y+35,'#a8c3de' if i==2 else '#92b8ba')
        if i==3:
            rect((x+70,y+21,x+91,y+35),'#fafbf4','#a5b1a9')
            for k in range(3):rect((x+74,y+24+k*4,x+85-k*2,y+25+k*4),'#74aa8b')
    else:
        for sx in (35,98):
            rect((x+sx,y+32,x+sx+34,y+47),'#768f84')
            rect((x+sx,y+27,x+sx+34,y+43),'#b3caa6' if i==5 else '#ded19f','#8da78d')
            rect((x+sx+3,y+29,x+sx+31,y+35),'#d4e4c2' if i==5 else '#eee2b6')
            rect((x+sx-2,y+32,x+sx+2,y+44),'#9fbfa3')
            rect((x+sx+32,y+32,x+sx+36,y+44),'#9fbfa3')
        if i==5:
            rect((x+75,y+34,x+90,y+46),'#f4faf2','#a3b5a4');plant(x+82,y+34)
        else:
            rect((x+76,y+27,x+87,y+38),'#f9f4d9','#b6ad80')
            rect((x+81,y+28,x+82,y+33),'#758e8a');rect((x+82,y+32,x+85,y+33),'#758e8a')

rect((0,151,479,168),'#bcd2c9')
rect((0,153,479,166),'#e8f1e9')
for x in range(0,480,16):d.line((x,154,x,166),fill='#cdded5')
for x in (80,240,400):
    rect((x-17,147,x+17,173),'#e8f1e9')
    for y in (149,169):d.line((x-16,y,x+16,y),fill='#b9d0c3')
    rect((x-17,169,x+17,192),'#e8f1e9')
    d.line((x-17,171,x-17,191),fill='#b9d0c3')
    d.line((x+17,171,x+17,191),fill='#b9d0c3')
OUT.parent.mkdir(parents=True,exist_ok=True)
im.resize((960,640),Image.Resampling.NEAREST).save(OUT)
metadata={'version':1,'method':'original local pixel drawing','source':'tools/draw_office_map.py',
          'size':[960,640],'sha256':hashlib.sha256(OUT.read_bytes()).hexdigest(),
          'license':'AP-Vibe Personal Noncommercial 1.0','provider_generated':False}
(OUT.parent/'office-map.source.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
print(json.dumps(metadata))
