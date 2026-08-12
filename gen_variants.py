"""
Generate a 2x (2048x3072) retina variant of after.png for srcset,
and regenerate a matching 'before' (degraded photocopy) version.
"""
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import numpy as np
import random

random.seed(7); np.random.seed(7)

# ---- 2x AFTER (retina srcset variant) ----
# Re-render at 2048x3072 with 2x font sizes so the geometry matches exactly.
W2, H2 = 2048, 3072
base = np.full((H2, W2, 3), [250, 243, 224], dtype=np.int32)
grad = np.linspace(0, 1, H2).reshape(H2, 1, 1)
edge_dark = (np.abs(grad - 0.5) * 2 * 10).astype(np.int32)
base = base - edge_dark
grain = np.random.normal(0, 2.5, (H2, W2, 3)).astype(np.int32)
base = base + grain
for _ in range(5):
    cx, cy = random.randint(0, W2), random.randint(0, H2)
    r = random.randint(160, 400)
    yy, xx = np.ogrid[:H2, :W2]
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    stain = np.clip(1 - dist / r, 0, 1) * 14
    base = base - stain[:, :, None] * np.array([8, 6, 4])
base = np.clip(base, 0, 255).astype(np.uint8)
paper = Image.fromarray(base)

BOLD = "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"
REG  = "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"
ITAL = "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf"
f_title  = ImageFont.truetype(BOLD, 156)
f_sub    = ImageFont.truetype(REG, 100)
f_instr  = ImageFont.truetype(ITAL, 92)
f_num    = ImageFont.truetype(BOLD, 108)
f_body   = ImageFont.truetype(REG, 104)
f_footer = ImageFont.truetype(REG, 68)
INK=(38,28,18); MUTED=(95,75,52); RUST=(135,55,32)
ML=260; MR=W2-260
draw = ImageDraw.Draw(paper)
draw.text((ML,190),"GRAMMAR EXERCISE",font=f_title,fill=INK)
draw.text((ML,376),"L E S S O N   X I I   ·   P A G E   4",font=f_sub,fill=MUTED)
draw.rectangle([ML,536,MR,544],fill=INK)
draw.rectangle([ML,560,MR,562],fill=MUTED)
draw.text((ML,630),"Underline the verb in each sentence.",font=f_instr,fill=MUTED)
sentences=[("1.","The squirrel gathered nuts all day long.",0.34,0.60),
           ("2.","Peter ran under the garden gate.",0.30,0.52),
           ("3.","Mrs. Rabbit baked a fresh pie.",0.45,0.72)]
y=920; ROW=500
for num,text,vs,ve in sentences:
    draw.text((ML,y),num,font=f_num,fill=INK)
    tx=ML+190
    draw.text((tx,y),text,font=f_body,fill=INK)
    tw=draw.textlength(text,font=f_body)
    ux0=tx+tw*vs; ux1=tx+tw*ve; uy=y+140
    xs=np.arange(ux0,ux1,6)
    for i,x in enumerate(xs):
        w=4.8*np.sin(i*0.55)
        draw.line([x,uy+w,x+6,uy-w],fill=RUST,width=12)
    draw.line([ux1,uy-8,ux1+10,uy+12],fill=RUST,width=10)
    y+=ROW
# illustration 2x
ill=Image.new("RGBA",(440,480),(0,0,0,0)); idr=ImageDraw.Draw(ill)
idr.ellipse([40,220,400,460],fill=(38,28,18,255))
idr.ellipse([260,120,420,280],fill=(38,28,18,255))
idr.polygon([(300,140),(280,0),(344,120)],fill=(38,28,18,255))
idr.polygon([(356,136),(370,10),(400,132)],fill=(38,28,18,255))
idr.ellipse([356,184,384,208],fill=(250,243,224,255))
idr.ellipse([10,300,110,390],fill=(38,28,18,255))
ill=ill.filter(ImageFilter.GaussianBlur(0.8))
paper.paste(ill,(ML,y+20),ill)
fy=H2-300
draw.rectangle([ML,fy-80,MR,fy-76],fill=MUTED)
draw.text((ML,fy),"Restored by Book Bash  ·  2026",font=f_footer,fill=MUTED)
pn="4"; pnw=draw.textlength(pn,font=f_num)
draw.text((MR-pnw,fy-16),pn,font=f_num,fill=MUTED)
paper.save("public/images/after@2x.png",optimize=True)
print("Saved after@2x.png")

# ---- BEFORE: degraded photocopy of the SAME worksheet ----
# Take after.png, add heavy degradation to simulate 5th-gen photocopy
src = Image.open("public/images/after.png").convert("RGB")
arr = np.array(src).astype(np.int32)
gray = arr.mean(axis=2)
# threshold to pure-ish ink
ink_mask = gray < 140
# photocopy degradation: grey background, splotches, skewed, low contrast
out = np.full_like(arr, 205)  # grey paper
out[ink_mask] = 60  # faded grey-black ink
# add copier noise bands
for _ in range(40):
    r0 = random.randint(0, arr.shape[0]-3)
    out[r0:r0+random.randint(1,3)] = out[r0:r0+1] - random.randint(8,25)
# splotches
for _ in range(60):
    cx,cy=random.randint(0,arr.shape[1]),random.randint(0,arr.shape[0])
    rr=random.randint(15,70)
    yy,xx=np.ogrid[:arr.shape[0],:arr.shape[1]]
    d=np.sqrt((xx-cx)**2+(yy-cy)**2)
    m=d<rr
    out[m]=np.clip(out[m]-random.randint(10,30),0,255)
# toner fade on right side (copier drift)
fade = np.linspace(0,30,arr.shape[1])[None,:,None]
out = np.clip(out + fade.astype(np.int32),0,255)
# grain
out = out + np.random.normal(0,8,out.shape).astype(np.int32)
out = np.clip(out,0,255).astype(np.uint8)
before = Image.fromarray(out)
# slight rotation (skewed scan)
before = before.rotate(1.2, fillcolor=205, resample=Image.BILINEAR)
before.save("public/images/before.webp", quality=82)
print("Saved before.webp")
