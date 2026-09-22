"""
模块名：dao.base_information

作用：
    DAO 层抽象基类模块，本身不对应任何数据库表，仅为各具体写入 DAO 定义统一的
    save_information() 接口（抽象基类，不可直接实例化）。
    数据库连接统一由 db.session 管理（SQLAlchemy Engine + QueuePool 连接池），
    子类使用 with session_scope() as session 获取会话，禁止自行创建连接。

主要成员：
    - BaseInformation：抽象基类（ABC），声明抽象方法 save_information(data)。

被谁使用：
    由 dao 包内各写库 DAO 继承实现：
    - dao/user.py 的 Information（user_information 表写入）；
    - dao/history.py 的 Information_history（history_information 表写入）；
    - dao/information.py 的 Information（session_information 表消息写入）；
    - dao/session.py 的 SessionDAO（会话管理写入）；
    - dao/read.py 的 Information_Read（只读 DAO，空实现以满足接口契约）。
"""
from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseInformation(ABC):
    """所有信息写入 DAO 的抽象基类。

    不对应具体 MySQL 表，仅约束子类统一实现 save_information()，
    使 service 层可以按同一接口形态调用不同表的写入逻辑。
    子类实例化位置见各子类模块（service/chat_service.py、memory/long_term.py、
    util/user.py 等）；本类无 __init__ 形参，也不持有数据库连接，
    会话由子类在方法内部通过 session_scope() 按需获取并自动提交/回滚。
    """

    @abstractmethod
    def save_information(self, data: Dict[str, Any]):
        """保存信息（抽象方法，子类必须覆写）。

        功能：由子类决定具体操作的数据库表与 SQL 语义（INSERT 或 upsert）。
        参数：
            data: 待持久化的字段字典，键值由各子类与调用方（service 层）约定，
                  通常包含 service 层传入的 user_id、session_id 及业务内容。
        返回：由子类约定（如 "success"/"false" 字符串或布尔值）。
        异常：基类默认实现直接抛出 NotImplementedError；子类内部一般捕获
              数据库异常并记录日志，不向上抛出。
        """
        raise NotImplementedError
