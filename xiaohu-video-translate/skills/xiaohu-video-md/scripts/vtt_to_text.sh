#!/bin/bash
# VTT/SRT 字幕清洗脚本
# 去除时间戳、序号、空行，输出纯文本（每行一句）

INPUT="$1"

if [ -z "$INPUT" ]; then
  echo "用法: $0 <字幕文件>"
  exit 1
fi

grep -v '^WEBVTT' "$INPUT" | grep -v 'NOTE' | grep -v '^[0-9]*$' | grep -v -e '-->' | grep -v '^STYLE' | grep -v '^Kind:' | grep -v '^Language:' | grep -v '^\s*$' | sed 's/^[[:space:]]*//' | sed '/^[[:space:]]*$/d'
