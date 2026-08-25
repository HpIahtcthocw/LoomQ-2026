"""Build the short judging walkthrough from committed Web screenshots."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "starter_kit" / "evidence" / "files" / "webui"
OUTPUT = ROOT / "starter_kit" / "evidence" / "files" / "loomq-walkthrough.gif"

STEPS = [
    ("01-opening.png", "1  零基础开场：先读懂，再开始计时"),
    ("02-pick.png", "2  选择一个现成实验，不用先写代码"),
    ("03-result.png", "3  电路、分布和人话解释在同一页"),
    ("04-ask.png", "4  也可以直接用中文描述想做的实验"),
    ("10-pending.png", "5  真机排队时先看同一电路的模拟结果"),
    ("08-hardware.png", "6  真机返回后对照理想结果和真实噪声"),
    ("05-quiz.png", "7  三道题检查是否真的理解了原理"),
    ("06-cert.png", "8  五分钟内完成并导出实验凭证"),
]

CANVAS = (1120, 760)
CAPTION_HEIGHT = 72


def font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def frame(path: Path, caption: str) -> Image.Image:
    image = Image.open(path).convert("RGB")
    available = (CANVAS[0], CANVAS[1] - CAPTION_HEIGHT)
    image.thumbnail(available, Image.Resampling.LANCZOS)

    canvas = Image.new("RGB", CANVAS, "#07111d")
    left = (CANVAS[0] - image.width) // 2
    top = (available[1] - image.height) // 2
    canvas.paste(image, (left, top))

    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, available[1], CANVAS[0], CANVAS[1]), fill="#0b1826")
    draw.text((36, available[1] + 19), caption, fill="#b9f5ff", font=font(27))
    return canvas


def main() -> None:
    frames = [frame(SOURCE / name, caption) for name, caption in STEPS]
    frames[0].save(
        OUTPUT,
        save_all=True,
        append_images=frames[1:],
        duration=[1700] * len(frames),
        loop=0,
        optimize=True,
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()
