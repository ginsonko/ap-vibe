# 宣传图与产品教程

- [一图入门 PNG](ap-vibe-guide.png)：可分享给用户，也可交给支持读图的 Codex 作为安装入口。
- [轻量预览](ap-vibe-guide-preview.webp)：适合浏览。
- [1080p 中文介绍与教程](ap-vibe-introduction.mp4)：约 3 分 26 秒，九段讲解，中文配音、字幕、原创轻背景音和像素动作。
- [字幕](ap-vibe-introduction.srt) · [章节时间](video-chapters.json) · [分镜与旁白](storyboard.json)

品牌场景插画使用 gpt-image-2 生成，之后本地排版；角色来自本项目已有图集。原始用户参考图不分发。教程包含明确标注的流程示意及真实独立演练项目界面，示意内容不作为性能数据。九套图集的来源与限制见 assets/characters 各自的 manifest。

中文配音为合成语音，背景和剪辑由本地脚本制作。没有模仿真实人物声音，没有使用第三方商业配乐。字体仅用于栅格化成品，不随仓库提供字体文件。

复现排版与剪辑见 tools/render_promotion.py；作者需要 Pillow、numpy、edge-tts、FFmpeg 和中文字体。这些是媒体制作工具，不是 AP-Vibe 的运行依赖。旁白生成需要网络。已生成片段可以继续合成，无需再次调用。

素材按项目个人非商业许可证分享。请保留来源，修改版说明修改内容。模型与执行器名称不表示官方授权或背书。
