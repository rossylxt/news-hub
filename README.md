# 个人资讯聚合台（真实数据自动抓取版）

一个纯前端展示 + GitHub Actions 自动抓取的个人资讯聚合网站。资讯数据来自真实 RSS 源和公开赛程 API，每 2 小时自动抓取一次并自动发布，全程免费、全自动，不需要你自己的服务器或电脑常开。

## 工作原理

```
GitHub Actions（定时任务，每2小时）
   │
   ├─ 运行 fetch_data.py
   │    ├─ 抓取多个真实 RSS 源（AI/商业/足球/NBA 新闻）
   │    ├─ 抓取 balldontlie / football-data.org 的赛程（重点赛事预告）
   │    ├─ 和仓库里已有的 data.json 合并去重、裁剪条数
   │    └─ 写出最新的 data.json
   │
   └─ 把 data.json 提交回仓库
        │
        └─ GitHub Pages（从 main 分支自动部署）自动更新线上页面
             │
             └─ 你打开网站时，index.html 通过 fetch('data.json') 加载最新数据
```

你完全不需要手动维护任何一条新闻——从数据抓取到网站更新全部自动完成。

## 文件结构

```
news-hub/
├── index.html                          页面结构 + 渲染/筛选/无限滚动/暗色模式逻辑（内联，单文件）
├── data.json                           抓取结果（初始为空，Actions 第一次运行后会自动填充真实数据）
├── fetch_data.py                       抓取脚本：拉取RSS源+赛程API，生成 data.json
├── requirements.txt                    fetch_data.py 依赖的 Python 库
└── .github/workflows/update-data.yml   GitHub Actions 定时任务配置
```

## 已接入的真实数据源

| 分类 | 数据源 |
|---|---|
| AI · 国内 | 量子位 |
| AI · 国际 | TechCrunch AI、VentureBeat AI |
| 商业 · 国内 | 36氪 |
| 商业 · 国际 | Yahoo Finance |
| 日本旅欧球员 | ゲキサカ（海外サッカー，按球员姓名关键词过滤）+ ゲキサカ（日本代表） |
| 日本高校足球 | ゲキサカ（高校＆大学） |
| 国际足球综合 | Yahoo Sports Soccer |
| NBA | Yahoo Sports NBA |
| 赛事预告 · 篮球 | balldontlie.io（NBA 赛程，需免费 API Key） |
| 赛事预告 · 足球 | football-data.org（英超/西甲/德甲/意甲/法甲/欧冠赛程，需免费 API Key） |

所有 RSS 源都是发布时已实际验证过能正常访问的公开订阅源。两个赛程 API 的 Key 是可选的——不配置也不会导致抓取失败，只是"重点赛事预告"板块会跳过对应的数据源（比如只配置了 NBA 的 Key，那就只有 NBA 赛程，没有足球赛程）。

## 部署步骤（一次性设置，大约10-15分钟）

### 第一步：创建 GitHub 仓库并上传代码

1. 如果还没有 GitHub 账号，先在 [github.com](https://github.com) 免费注册一个。
2. 新建一个仓库（New repository），比如叫 `news-hub`，设为 Public（GitHub Pages 免费版需要公开仓库；如果你有 GitHub Pro，Private 仓库也可以用 Pages）。
3. 把本项目的所有文件（包括 `.github` 文件夹，注意这是隐藏文件夹，用 Finder/资源管理器要开启"显示隐藏文件"，或者直接用 `git` 命令上传就不会遗漏）上传到这个仓库，保持目录结构不变。

最简单的方式是用 Git 命令行（在项目文件夹里依次执行，把 `你的用户名/news-hub` 换成你自己的仓库地址）：

```bash
git init
git add .
git commit -m "init: 个人资讯聚合台"
git branch -M main
git remote add origin https://github.com/你的用户名/news-hub.git
git push -u origin main
```

### 第二步：开启 GitHub Pages

1. 进入仓库页面 → Settings（设置） → 左侧菜单 Pages。
2. Source 选择 "Deploy from a branch"，Branch 选择 `main`，目录选择 `/ (root)`，保存。
3. 稍等片刻，页面顶部会出现你的网站地址，形如 `https://你的用户名.github.io/news-hub/`。

### 第三步：允许 Actions 把抓取结果提交回仓库

1. 仓库 Settings → 左侧菜单 Actions → General。
2. 拉到底部 "Workflow permissions"，选择 **"Read and write permissions"**，保存。
   （这一步必须做，否则定时任务抓到数据后无法提交 `data.json`，网站数据不会更新。）

### 第四步（可选，但强烈建议）：配置赛事预告的免费 API Key

不配置的话，网站其他部分完全正常，只是"重点赛事预告"板块会是空的。

**NBA 赛程（balldontlie）**：
1. 打开 [balldontlie.io](https://www.balldontlie.io/) 注册一个免费账号，在个人中心找到你的 API Key。
2. 回到 GitHub 仓库 Settings → Secrets and variables → Actions → New repository secret。
3. Name 填 `BALLDONTLIE_API_KEY`，Value 填你刚才拿到的 Key，保存。

**足球赛程（football-data.org）**：
1. 打开 [football-data.org/client/register](https://www.football-data.org/client/register) 注册免费账号，邮件里会收到你的 API Token。
2. 同样在 GitHub 仓库 Secrets 里新建一个，Name 填 `FOOTBALL_DATA_API_KEY`，Value 填你的 Token。

两个都是免费额度，个人使用完全够用（football-data.org 免费版每天100次请求，balldontlie 免费版每分钟5次请求，我们的抓取频率远低于这个上限）。

### 第五步：手动触发一次抓取，检查是否成功

1. 仓库页面 → Actions 标签页 → 左侧选择 "抓取资讯数据" 这个工作流。
2. 点击右侧 "Run workflow" 按钮手动触发一次（不用等 2 小时的定时任务）。
3. 等待一两分钟，刷新页面看运行状态。如果是绿色对勾 ✅ 说明成功；点进去可以看到详细日志，包括每个数据源抓到了多少条、有没有报错。
4. 成功后访问你的网站地址（第二步里拿到的那个 `github.io` 链接），应该就能看到真实抓取到的资讯了，页面顶部筛选栏下方会显示"数据更新于 xxxx-xx-xx xx:xx"。

之后网站会每 2 小时自动更新一次，你不需要做任何事情。

## 本地测试（可选）

因为 `index.html` 用 `fetch()` 异步加载 `data.json`，浏览器出于安全限制，**不支持直接双击打开 `index.html` 来加载数据**（会报 CORS 错误，页面会停在"正在加载最新资讯…"或显示错误提示）。本地测试请启动一个简易服务器：

```bash
# 在项目文件夹里执行
python3 -m http.server 8000
# 然后浏览器打开 http://localhost:8000
```

如果本地想跑一次真实抓取生成数据用于测试：

```bash
pip install -r requirements.txt
python3 fetch_data.py
# 会在当前目录生成/更新 data.json
```

## 如何调整数据源、分类或抓取频率

打开 `fetch_data.py`，主要看这几处（都有详细注释）：

- **`FEED_SOURCES`**：每个分类对应的 RSS 源列表，想换源/加源直接在对应分类的列表里增删一项 `{"url": "...", "source": "..."}` 即可。
- **`JAPAN_PLAYER_KEYWORDS`**：用来从"海外サッカー"综合报道里筛出日本旅欧球员相关新闻的球员姓名关键词，球员转会/退役后可以增删调整。
- **`IMPORTANT_KEYWORDS`**：判定"重要新闻"标记的关键词列表。
- **`FOOTBALL_DATA_COMPETITIONS`**：赛事预告里包含哪些足球联赛/赛事。
- **`MAX_ITEMS_PER_CATEGORY`** / **`MAX_MATCH_PREVIEWS`**：单分类最多保留多少条资讯 / 最多展示多少场赛事预告。

想调整抓取频率（默认每2小时），改 `.github/workflows/update-data.yml` 里的这一行：

```yaml
schedule:
  - cron: "0 */2 * * *"   # 改成你想要的频率，比如 "0 */6 * * *" 就是每6小时一次
```

新增一个全新的资讯分类（比如以后想加"电竞新闻"），需要三处同步改：
1. `fetch_data.py` 的 `FEED_SOURCES` 里加一个新的 category key 和对应的 RSS 源。
2. `index.html` 里搜索 `CATEGORY_META`，加一项分组/展示文案/配色。
3. `index.html` 里搜索 `FILTER_GROUPS`，加一个筛选按钮。

## 已知局限 / 后续可以优化的地方

- **日本旅欧球员分类目前靠关键词过滤实现**：因为没有找到专门只报道"日本旅欧球员"的现成RSS源，所以是从一个欧洲足球综合新闻源里，用球员姓名关键词筛出相关报道。这意味着：只有关键词列表里出现过的球员，其相关报道才会被归类进来；新崛起的旅欧球员需要你自己去 `JAPAN_PLAYER_KEYWORDS` 里加上名字。
- **日本高校足球暂无独立赛程数据源**：目前"重点赛事预告"只覆盖 NBA 和欧洲五大联赛+欧冠，没有接入日本高校足球赛程（没找到可靠的免费公开API），如果你有更好的数据源线索，可以在 `fetch_data.py` 里参照 `fetch_football_previews()` 的写法自行扩展。
- **摘要来自RSS原文的简介字段**：不同媒体摘要长度、质量不一，脚本会做基础的HTML清理和截断（120字），但不会做二次改写。
- **部分海外网站可能因反爬虫策略偶尔抓取失败**：脚本对每个源都做了独立的异常处理，单个源失败不会影响其他源，Actions 运行日志里能看到具体是哪个源出了问题。

## 已实现的功能一览

- 6 大资讯类别（AI 国内/国际、商业 国内/国际、日本旅欧球员、日本高校足球、国际足球、NBA）+ 独立的"重点赛事预告"板块，数据来自真实公开数据源，每 2 小时自动更新。
- 顶部分类筛选（全部 / 单类切换）。
- 资讯默认按发布时间倒序排列，重要新闻（标题命中关键词）优先置顶。
- 无限滚动加载，加载中有简易 loading 提示，到底显示"没有更多资讯"。
- 点击卡片在新标签页打开原文链接，网站本身不存储原文内容。
- 暗色 / 浅色模式一键切换（默认跟随系统偏好）。
- 响应式布局，适配桌面、平板、手机。
- 页面顶部显示数据最后更新时间。

## 常见问题排查

**网站打开后一直卡在"正在加载最新资讯…"或提示加载失败**
- 大概率是 Actions 还没成功运行过一次，`data.json` 还是初始的空数据。去 Actions 标签页手动 Run workflow 一次，看日志有没有报错。
- 如果是在本地双击打开 `index.html`（file:// 协议），fetch 本地文件会被浏览器拦截，这是正常现象，请用本地服务器或直接访问部署好的网站。

**Actions 运行成功了，但网站没更新**
- 检查 GitHub Pages 的 Source 设置是否是 "Deploy from a branch: main"。如果 data.json 有更新但 Pages 没重新部署，去 Actions 标签页看看有没有自动触发 Pages 的部署记录；一般 push 到 main 分支会自动触发。
- 浏览器强制刷新一下（Ctrl/Cmd + Shift + R），有时候是浏览器缓存了旧的 data.json。

**Actions 报错说没有权限 push**
- 回到"第三步：允许 Actions 把抓取结果提交回仓库"，确认 Workflow permissions 已经设置为 "Read and write permissions"。

**某个分类长期没有新内容**
- 该分类对应的 RSS 源可能改版或失效了，去 Actions 运行日志里看抓取输出，确认具体是哪个源返回了 0 条或报错，然后去 `fetch_data.py` 的 `FEED_SOURCES` 里更新对应的 URL。
 news-hub
