# MoneyPrinterTurbo 手动部署指南

## 📋 前置要求

- **操作系统**: Windows 10+ / macOS 11.0+ / Linux
- **Python**: 3.11 或更高版本
- **CPU**: 建议 4核 或以上
- **内存**: 建议 4GB 或以上
- **网络**: 需要稳定的网络连接（某些功能需要访问外部API）

## 🚀 部署步骤

### 步骤 1: 克隆项目代码

```bash
git clone https://github.com/harry0703/MoneyPrinterTurbo.git
cd MoneyPrinterTurbo
```

> ⚠️ **重要提示**: 
> - 项目路径中**不要包含中文、特殊字符或空格**
> - 例如：`D:\code4\51_computer\maven\MoneyPrinterTurbo` ✅
> - 避免：`D:\我的项目\MoneyPrinterTurbo` ❌

### 步骤 2: 创建 Python 虚拟环境

#### 方式一：使用 Conda（推荐）

1. 安装 Conda（如果还没有）:
   - 下载地址: https://conda.io/projects/conda/en/latest/user-guide/install/index.html

2. 创建虚拟环境:
```bash
conda create -n MoneyPrinterTurbo python=3.11
conda activate MoneyPrinterTurbo
```

#### 方式二：使用 venv（Python 内置）

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
```

**macOS/Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### 步骤 3: 安装 Python 依赖

在激活的虚拟环境中执行：

```bash
pip install -r requirements.txt
```

> 💡 **提示**: 如果下载速度慢，可以使用国内镜像源：
> ```bash
> pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
> ```

### 步骤 4: 安装 ImageMagick

ImageMagick 用于处理图像和生成字幕。

#### Windows

1. **下载 ImageMagick**:
   - 访问: https://imagemagick.org/script/download.php
   - ⚠️ **重要**: 必须选择 **静态库版本** (static)
   - 例如: `ImageMagick-7.1.1-32-Q16-x64-static.exe`

2. **安装**:
   - 运行下载的安装程序
   - ⚠️ **注意**: 安装时**不要修改默认安装路径**

3. **配置路径** (如果需要):
   - 如果 ImageMagick 没有自动检测到，需要手动配置
   - 编辑 `config.toml` 文件，设置 `imagemagick_path`
   - 例如: `imagemagick_path = "C:\\Program Files\\ImageMagick-7.1.1-Q16-HDRI\\magick.exe"`

#### macOS

```bash
brew install imagemagick
```

#### Ubuntu/Debian

```bash
sudo apt-get update
sudo apt-get install imagemagick
```

#### CentOS/RHEL

```bash
sudo yum install ImageMagick
```

### 步骤 5: 配置 FFmpeg（通常自动处理）

FFmpeg 用于视频处理，通常会被自动下载和检测。

如果遇到错误 `RuntimeError: No ffmpeg exe could be found`:

1. **手动下载 FFmpeg**:
   - Windows: https://www.gyan.dev/ffmpeg/builds/
   - 解压到某个目录

2. **配置路径**:
   - 编辑 `config.toml` 文件
   - 设置 `ffmpeg_path` 为你的实际路径
   - 例如: `ffmpeg_path = "C:\\Users\\YourName\\Downloads\\ffmpeg.exe"`

### 步骤 6: 创建配置文件

1. **复制配置文件模板**:
```bash
# Windows
copy config.example.toml config.toml

# macOS/Linux
cp config.example.toml config.toml
```

2. **编辑配置文件** (`config.toml`):

#### 必须配置项：

**① 视频素材 API Key** (至少配置一个):
```toml
# Pexels API Key (推荐)
pexels_api_keys = ["你的Pexels_API_Key"]
# 注册地址: https://www.pexels.com/api/

# 或使用 Pixabay
pixabay_api_keys = ["你的Pixabay_API_Key"]
# 注册地址: https://pixabay.com/api/docs/
```

**② LLM 提供商配置** (选择一个):

```toml
# 选项1: OpenAI
llm_provider = "openai"
openai_api_key = "你的OpenAI_API_Key"
openai_model_name = "gpt-4o-mini"

# 选项2: DeepSeek (国内推荐，无需VPN)
llm_provider = "deepseek"
deepseek_api_key = "你的DeepSeek_API_Key"
deepseek_model_name = "deepseek-chat"

# 选项3: Moonshot (国内推荐，无需VPN)
llm_provider = "moonshot"
moonshot_api_key = "你的Moonshot_API_Key"
moonshot_model_name = "moonshot-v1-8k"

# 选项4: 通义千问
llm_provider = "qwen"
qwen_api_key = "你的通义千问_API_Key"
qwen_model_name = "qwen-max"
```

#### 可选配置项：

```toml
# 字幕生成方式: "edge" (快速) 或 "whisper" (质量更好)
subtitle_provider = "edge"

# Azure 语音合成 (可选，需要API Key)
[azure]
speech_key = "你的Azure_Speech_Key"
speech_region = "你的Azure_区域"
```

> 💡 **国内用户建议**:
> - LLM: 使用 **DeepSeek** 或 **Moonshot**（国内可直接访问，注册送额度）
> - 字幕: 使用 **edge** 模式（速度快，无需额外下载）

### 步骤 7: 启动服务

#### 方式一：启动 Web 界面（推荐新手）

**Windows:**
```bash
webui.bat
```

**macOS/Linux:**
```bash
sh webui.sh
```

启动后会自动打开浏览器，访问 Web 界面。

> 💡 如果浏览器打开是空白，建议使用 **Chrome** 或 **Edge** 浏览器

#### 方式二：启动 API 服务

```bash
python main.py
```

启动后可以访问：
- **API 文档**: http://127.0.0.1:8080/docs
- **ReDoc**: http://127.0.0.1:8080/redoc

### 步骤 8: 验证部署

1. **检查 Web 界面**:
   - 访问 http://localhost:8501 (Streamlit 默认端口)
   - 应该能看到项目界面

2. **检查 API 服务**:
   - 访问 http://127.0.0.1:8080/docs
   - 应该能看到 Swagger API 文档

3. **测试生成视频**:
   - 在 Web 界面输入一个主题
   - 点击生成，观察是否能正常生成视频

## 🔧 常见问题排查

### 问题 1: 无法安装依赖

**解决方案**:
```bash
# 升级 pip
pip install --upgrade pip

# 使用国内镜像
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 问题 2: ImageMagick 安全策略错误

**错误信息**: `ImageMagick的安全策略阻止了与临时文件相关的操作`

**解决方案**:
1. 找到 ImageMagick 的 `policy.xml` 配置文件
   - Windows: `C:\Program Files\ImageMagick-7.x.x-Q16\config\policy.xml`
   - Linux: `/etc/ImageMagick-7/policy.xml`
2. 找到包含 `pattern="@"` 的行
3. 将 `rights="none"` 改为 `rights="read|write"`

### 问题 3: Whisper 模型下载失败

如果使用 `whisper` 字幕模式，需要下载模型（约 3GB）。

**国内用户解决方案**:
1. 从网盘下载模型:
   - 百度网盘: https://pan.baidu.com/s/11h3Q6tsDtjQKTjUu3sc5cA?pwd=xjs9
   - 夸克网盘: https://pan.quark.cn/s/3ee3d991d64b
2. 解压后放到项目目录:
   ```
   MoneyPrinterTurbo/
     └── models/
         └── whisper-large-v3/
             ├── config.json
             ├── model.bin
             └── ...
   ```

### 问题 4: 端口被占用

如果 8080 或 8501 端口被占用，可以修改配置：

**修改 API 端口** (`app/config/config.py` 或环境变量):
```python
listen_port = 8081  # 改为其他端口
```

**修改 Streamlit 端口**:
```bash
streamlit run ./webui/Main.py --server.port 8502
```

### 问题 5: 网络连接问题

如果无法访问外部 API（如 Pexels、OpenAI）:

1. **检查网络连接**
2. **配置代理** (如果需要):
   ```toml
   [proxy]
   http = "http://your-proxy:port"
   https = "http://your-proxy:port"
   ```
3. **使用国内服务商**: DeepSeek、Moonshot、通义千问等

## 📝 配置文件说明

完整的配置选项请参考 `config.example.toml` 文件中的注释说明。

主要配置项：
- `pexels_api_keys`: Pexels API 密钥列表
- `llm_provider`: LLM 提供商
- `subtitle_provider`: 字幕生成方式
- `material_directory`: 视频素材存储位置
- `enable_redis`: 是否启用 Redis（用于任务状态管理）

## 🎯 下一步

部署成功后，你可以：

1. **通过 Web 界面使用**:
   - 输入视频主题
   - 选择视频尺寸（竖屏/横屏）
   - 生成视频

2. **通过 API 使用**:
   - 查看 API 文档: http://127.0.0.1:8080/docs
   - 使用 Postman 或其他工具调用 API

3. **自定义配置**:
   - 调整字幕样式
   - 添加自定义背景音乐
   - 使用本地视频素材

## 📚 更多资源

- **项目地址**: https://github.com/harry0703/MoneyPrinterTurbo
- **问题反馈**: https://github.com/harry0703/MoneyPrinterTurbo/issues
- **视频教程**: 
  - 完整演示: https://v.douyin.com/iFhnwsKY/
  - Windows部署: https://v.douyin.com/iFyjoW3M

---

**祝使用愉快！** 🎉

