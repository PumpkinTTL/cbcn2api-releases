/* ============================================================================
 * WorkBuddy 配置模块（独立 JS，可扩展）
 * ----------------------------------------------------------------------------
 * 设计目标：WorkBuddy 相关的所有可配置功能都挂载在这个模块里，与主 index.html
 * 解耦。目前包含「模型降级顺序」配置；后续新功能（如队列策略、限流探测参数等）
 * 继续往这里加。
 *
 * 使用方式：
 *   1. index.html 引入 <script src="workbuddy_config.js"></script>（在 Vue 之后）
 *   2. 主 setup() 里调用 const wbConfig = window.WorkBuddyConfig.create()
 *   3. 把 wbConfig 的状态/方法合并进 setup 的 return
 *   4. 面板 HTML 在 index.html（复用现有 modal 风格），绑定 wbConfig 暴露的字段
 *
 * 降级顺序语义：
 *   - degradeEnabled：总开关
 *   - degradeOrder：全局降级顺序（跨厂商），如 [kimi-k3-1, glm-5.3, deepseek-v4-pro, …]
 *   - 单模型限流（6004）与排队（6020）共用同一条顺序
 *   - 请求的模型被限/排队时，网关在同一账号上按这条顺序往后降级，链尽才换号
 * ========================================================================== */

(function () {
  'use strict';

  function create() {
    const { ref } = Vue;

    // ── 状态 ──
    const degradeOpen = ref(false);              // 面板是否打开
    const degradeEnabled = ref(true);            // 降级总开关
    const degradeOrder = ref([]);                // 全局降级顺序（模型 id 数组）
    const degradeSaving = ref(false);
    const degradeLoading = ref(false);
    const degradeError = ref('');
    const degradeModels = ref([]);               // 可选模型列表（主 app 注入，含 id/name）
    const degradeAddOpen = ref(false);           // 「添加模型」下拉是否展开
    const degradeAddSel = ref('');               // 待添加的模型 id

    // ── 方法 ──
    function setDegradeModels(models) {
      degradeModels.value = models || [];
    }

    function openDegradeConfig() {
      degradeError.value = '';
      degradeAddOpen.value = false;
      degradeAddSel.value = '';
      degradeOpen.value = true;
      loadDegradeConfig();
    }

    function closeDegradeConfig() {
      degradeOpen.value = false;
      degradeAddOpen.value = false;
      degradeAddSel.value = '';
    }

    async function loadDegradeConfig() {
      degradeLoading.value = true;
      degradeError.value = '';
      try {
        const raw = await pywebview.api.get_degrade_config();
        const cfg = JSON.parse(raw);
        degradeEnabled.value = !!cfg.enabled;
        degradeOrder.value = Array.isArray(cfg.order) ? cfg.order.slice() : [];
      } catch (e) {
        degradeError.value = '加载降级配置失败: ' + e;
      } finally {
        degradeLoading.value = false;
      }
    }

    async function saveDegradeConfig() {
      degradeSaving.value = true;
      degradeError.value = '';
      try {
        const payload = JSON.stringify({
          enabled: degradeEnabled.value,
          order: degradeOrder.value.slice(),
        });
        const raw = await pywebview.api.set_degrade_config(payload);
        const r = JSON.parse(raw);
        if (r.error) {
          degradeError.value = r.error;
        } else {
          // 用后端清洗后的结果回填，保证前后一致
          degradeEnabled.value = !!r.enabled;
          degradeOrder.value = Array.isArray(r.order) ? r.order.slice() : [];
          closeDegradeConfig();
          if (window.showToast) window.showToast('降级配置已保存', 'success');
        }
      } catch (e) {
        degradeError.value = '保存降级配置失败: ' + e;
      } finally {
        degradeSaving.value = false;
      }
    }

    function orderCount() {
      return degradeOrder.value.length;
    }

    function modelName(id) {
      const m = degradeModels.value.find(x => x.id === id);
      return m ? (m.name || m.id) : id;
    }

    // 尚未加入降级顺序的模型（auto 是路由模型，不可作为降级目标）
    function unlistedModels() {
      return degradeModels.value.filter(m =>
        m.id && m.id !== 'auto' && !degradeOrder.value.includes(m.id)
      );
    }

    function addOrderModel(id) {
      if (!id || id === 'auto' || degradeOrder.value.includes(id)) return;
      degradeOrder.value.push(id);
      degradeAddSel.value = '';
      degradeAddOpen.value = false;
    }

    function removeOrderModel(id) {
      const i = degradeOrder.value.indexOf(id);
      if (i >= 0) degradeOrder.value.splice(i, 1);
    }

    function moveOrderModel(index, dir) {
      const j = index + dir;
      if (j < 0 || j >= degradeOrder.value.length) return;
      const tmp = degradeOrder.value[index];
      degradeOrder.value[index] = degradeOrder.value[j];
      degradeOrder.value[j] = tmp;
    }

    function toggleDegradeAdd() {
      degradeAddOpen.value = !degradeAddOpen.value;
      degradeAddSel.value = '';
    }

    return {
      // 状态
      degradeOpen, degradeEnabled, degradeOrder, degradeSaving, degradeLoading,
      degradeError, degradeModels, degradeAddOpen, degradeAddSel,
      // 方法
      setDegradeModels, openDegradeConfig, closeDegradeConfig, loadDegradeConfig,
      saveDegradeConfig, orderCount, modelName, unlistedModels, addOrderModel,
      removeOrderModel, moveOrderModel, toggleDegradeAdd,
    };
  }

  window.WorkBuddyConfig = { create };
})();
