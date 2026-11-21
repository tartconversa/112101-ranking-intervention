"""推荐治理干预双时间数仓。

三层事实严格分离：

* 政策允许范围 —— ``dim_policy_scope``（政策团队定义的干预依据与处置边界）；
* 配置计划     —— ``dim_binding``（运营团队配置的策略版本与渠道授权）；
* 真实分发     —— ``fact_deployment`` / ``fact_exposure``（实际投放与曝光事实）。

双时间：业务时间（effective/occurred）与系统记录时间（recorded_at，来自
``ingestion_log.csv``）相互独立，支持历史时点查询且不按当前配置重写历史。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
