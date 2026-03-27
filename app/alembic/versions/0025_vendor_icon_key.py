from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0025_vendor_icon_key"
down_revision = "0024_usage_request_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vendors", sa.Column("icon_key", sa.String(length=100), nullable=True)
    )
    op.execute(
        """
        UPDATE vendors
        SET icon_key = CASE lower(btrim(key))
            WHEN 'openai' THEN 'openai'
            WHEN 'anthropic' THEN 'anthropic'
            WHEN 'google' THEN 'google'
            WHEN 'deepseek' THEN 'deepseek'
            WHEN 'zhipu' THEN 'zhipu'
            WHEN 'chatglm' THEN 'zhipu'
            WHEN 'glm' THEN 'zhipu'
            WHEN 'zai' THEN 'zhipu'
            WHEN 'z.ai' THEN 'zhipu'
            WHEN 'azure' THEN 'azure'
            WHEN 'microsoft' THEN 'azure'
            WHEN 'xai' THEN 'xai'
            WHEN 'grok' THEN 'xai'
            WHEN 'moonshot' THEN 'kimi'
            WHEN 'kimi' THEN 'kimi'
            WHEN 'aliyun' THEN 'alibaba'
            WHEN 'alibaba' THEN 'alibaba'
            WHEN 'alibaba_cloud' THEN 'alibaba'
            WHEN 'tencent' THEN 'tencent'
            WHEN 'hunyuan' THEN 'tencent'
            WHEN 'baidu' THEN 'baidu'
            WHEN 'wenxin' THEN 'baidu'
            WHEN 'ernie' THEN 'baidu'
            ELSE NULL
        END
        """
    )


def downgrade() -> None:
    op.drop_column("vendors", "icon_key")
