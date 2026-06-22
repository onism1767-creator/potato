"""pipeline —— 核心流水线,真实形状是菱形 DAG(决策记录见 ARCHITECTURE.md):

            ┌─ extract(纯)──┐
probe → ①raw                ├─→ score(纯)→ report(render 纯 / write IO)
            └─ verify(IO)───┘

extract 与 verify 都只消费原始层;score 消费两者产物;每段职责单一。
"""
