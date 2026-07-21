"use strict";

/**
 * 芙宁娜桌宠（Live2D 版）：右下角贴内容区的 Live2D 立绘 + 气泡播报。
 * 状态源：GET /pet-status（今日卡片进度、待复习数）。
 * 事件钩子：反馈点赞、复习评分、手动刷新（HTMX afterRequest）。
 * 降级：WebGL / 模型加载失败时回退为静态立绘。
 */

const PET = {
  modelUrl: "/static/live2d/furina/Furina.model3.json",
  fallbackImgs: {
    idle: "/static/pet/idle.png",
    happy: "/static/pet/happy.png",
    remind: "/static/pet/remind.png",
    celebrate: "/static/pet/celebrate.png",
  },
  width: 210,
  height: 270,
};

// 芙宁娜口吻台词库：歌剧/剧目语汇，贴合站点"歌剧院"设定
const PET_LINES = {
  greeting_full: (n) => `今日 ${n} 幕剧目全部就位，观众席请就座！`,
  greeting_partial: (n, quota) => `已献映 ${n}/${quota} 幕，后厨还在排演剩下的剧目。`,
  greeting_cooking: () => `今日的剧目还在筹备中……耐心是观众的美德。`,
  review_remind: (n) => `还有 ${n} 张卡片等你复习，谢幕前可别离场哦。`,
  feedback_like: () => `嗯？这张卡片合你心意？品味不错嘛。`,
  feedback_dislike: () => `被喝倒彩了……好吧，明天的选角会更用心。`,
  review_grade: () => `评分收到！这张卡片离谢幕又近了一步。`,
  refresh: () => `重新排演开始！请稍候片刻。`,
  chatter: [
    "在这个歌剧院里，学习才是永不落幕的演出。",
    "知识如流水，温故而知新——这可是你说的。",
    "需要我为你报幕吗？今天的剧目可精彩了。",
    "啧，别盯着我看，去看卡片。",
    "每一次复习，都是一次安可返场。",
  ],
};

const BUBBLE_MS = 6000; // 气泡停留时长
const REMIND_INTERVAL_MS = 3 * 60 * 1000; // 复习提醒间隔

const pet = {
  el: null,
  stage: null, // 立绘容器（canvas 或 img 的父级）
  img: null, // 降级静态图
  bubble: null,
  live2d: false,
  timer: null,
  queue: Promise.resolve(),

  /** 表情反应：Live2D 模式播容器动画，静态模式换立绘差分 */
  setFace(name) {
    if (this.live2d) {
      const anim = name === "celebrate" || name === "happy" ? "pet-react-bounce" : "pet-react-wiggle";
      if (name !== "idle") {
        this.stage.classList.remove("pet-react-bounce", "pet-react-wiggle");
        void this.stage.offsetWidth; // 重启动画
        this.stage.classList.add(anim);
        this.stage.addEventListener(
          "animationend",
          () => this.stage.classList.remove(anim),
          { once: true }
        );
      }
    } else if (this.img) {
      this.img.src = PET.fallbackImgs[name] || PET.fallbackImgs.idle;
    }
  },

  /** 气泡播报：排队执行，避免多条消息互相覆盖 */
  say(text, face = "idle") {
    this.queue = this.queue.then(
      () =>
        new Promise((resolve) => {
          this.setFace(face);
          this.bubble.textContent = text;
          this.bubble.classList.add("show");
          clearTimeout(this.timer);
          this.timer = setTimeout(() => {
            this.bubble.classList.remove("show");
            setTimeout(resolve, 300); // 等淡出动画结束再播下一条
          }, BUBBLE_MS);
        })
    );
  },
};

function initPet() {
  const wrap = document.createElement("div");
  wrap.id = "furina-pet";
  wrap.innerHTML = `
    <div class="pet-bubble" role="status"></div>
    <div class="pet-stage" title="芙宁娜"></div>
  `;
  document.body.appendChild(wrap);
  pet.el = wrap;
  pet.stage = wrap.querySelector(".pet-stage");
  pet.bubble = wrap.querySelector(".pet-bubble");

  initLive2D().catch(() => initFallbackImage());

  pet.stage.addEventListener("click", () => {
    const lines = PET_LINES.chatter;
    pet.say(lines[Math.floor(Math.random() * lines.length)], "happy");
  });

  greet();
  scheduleReviewReminder();
  bindEventHooks();
}

async function initLive2D() {
  if (!window.PIXI || !PIXI.live2d) throw new Error("Live2D 依赖未加载");

  const canvas = document.createElement("canvas");
  canvas.className = "pet-canvas";
  pet.stage.appendChild(canvas);

  const app = new PIXI.Application({
    view: canvas,
    transparent: true,
    autoDensity: true,
    antialias: true,
    width: PET.width,
    height: PET.height,
  });

  const model = await PIXI.live2d.Live2DModel.from(PET.modelUrl, {
    autoFocus: true, // 视线跟随鼠标
  });
  app.stage.addChild(model);

  // 缩放至画布大小并水平居中、底部对齐
  const scale = Math.min(PET.width / model.width, PET.height / model.height);
  model.scale.set(scale);
  model.x = (PET.width - model.width) / 2;
  model.y = PET.height - model.height;

  pet.live2d = true;
}

/** 静态立绘降级：WebGL 不可用或模型缺失时保住基本体验 */
function initFallbackImage() {
  pet.stage.innerHTML = ""; // 清掉可能半初始化的 canvas
  const img = document.createElement("img");
  img.className = "pet-avatar";
  img.src = PET.fallbackImgs.idle;
  img.alt = "芙宁娜桌宠";
  pet.stage.appendChild(img);
  pet.img = img;
}

async function greet() {
  let status = null;
  try {
    const resp = await fetch("/pet-status");
    status = await resp.json();
  } catch {
    return; // 接口不可用时保持静默，不影响页面
  }
  if (status.cooking) {
    pet.say(PET_LINES.greeting_cooking(), "remind");
  } else if (status.cards >= status.quota) {
    pet.say(PET_LINES.greeting_full(status.cards), "happy");
  } else {
    pet.say(PET_LINES.greeting_partial(status.cards, status.quota));
  }
}

async function scheduleReviewReminder() {
  setInterval(async () => {
    if (document.hidden) return;
    try {
      const resp = await fetch("/pet-status");
      const status = await resp.json();
      if (status.review_due > 0) {
        pet.say(PET_LINES.review_remind(status.review_due), "remind");
      }
    } catch {
      /* 静默 */
    }
  }, REMIND_INTERVAL_MS);
}

/** 监听 HTMX 请求结果，对学习行为做出反应 */
function bindEventHooks() {
  document.body.addEventListener("htmx:afterRequest", (evt) => {
    const path = evt.detail?.requestConfig?.path || "";
    if (!evt.detail?.successful) return;
    if (path.startsWith("/feedback/")) {
      const liked = path.endsWith("/up") || path.endsWith("/mastered");
      pet.say(
        liked ? PET_LINES.feedback_like() : PET_LINES.feedback_dislike(),
        liked ? "celebrate" : "remind"
      );
    } else if (/^\/review\/\d+\/grade\//.test(path)) {
      pet.say(PET_LINES.review_grade(), "celebrate");
    } else if (path === "/refresh") {
      pet.say(PET_LINES.refresh(), "remind");
    }
  });
}

document.addEventListener("DOMContentLoaded", initPet);
