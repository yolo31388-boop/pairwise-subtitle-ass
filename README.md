# pairwise-subtitle-ass

我们的字幕组反馈导入字幕整体晚 2 秒，24fps 转 25fps 的片子越往后期偏得越多；两条字幕时间重叠时后一条直接把前一条覆盖，导出 ASS 后样式也丢了；SRT 里的 `<i>`、`<font color>` 标签被原样写进 ASS，播放器显示成方块；字体名带空格时 Style 行解析失败；多语言双语字幕需要分别放到不同 layer；空字幕文件导出后播放器报错。subtitle/ass.py 是核心，tests/test_ass.py 15个红。要修的是：解析 SRT 时间码时兼容逗号、点和 BOM；按帧率换算并做累计误差校正；重叠字幕自动分配到下一个 layer 而不是覆盖；剥离 SRT 标签并转换成 ASS override；导出合法 Script Info、PlayRes、Style 和 Events；字体名带空格时加引号；空字幕也输出可解析的头部。

## 运行

```bash
python -m pytest tests -q
```

初始状态：15 个用例失败。修复实现使全部用例通过，不要修改 `tests/` 下的测试。
