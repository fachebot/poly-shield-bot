# 代理钱包模式配置说明

本文用于说明 `signer`、`funder`、`user address` 三者在不同 `POLY_SIGNATURE_TYPE` 下的关系，以及本项目当前的启动前校验行为。

私钥存储后端（用于替代环境变量明文私钥）：

- Windows: DPAPI
- Linux: keyring / SecretService
- 可通过 `POLY_SECRET_STORE_BACKEND` 强制指定后端（`dpapi` 或 `keyring`）

## 术语

- `signer`：由私钥推导出的 EOA 地址（实际执行签名的地址）。
- `funder`：提交交易时的出资/代理钱包地址（环境变量 `POLY_FUNDER`）。
- `user address`：用于官方 positions 查询的用户地址。

## 地址关系规则

| signature_type | 模式                    | signer       | funder             | user address(项目内推导)          |
| -------------- | ----------------------- | ------------ | ------------------ | --------------------------------- |
| `0` 或未设置   | 直连钱包                | 私钥对应地址 | 建议与 signer 一致 | signer（无 signer 时回退 funder） |
| `1`            | 代理钱包（Poly Proxy）  | 私钥对应地址 | 代理钱包地址       | funder                            |
| `2`            | 代理钱包（Gnosis Safe） | 私钥对应地址 | Safe/代理地址      | funder                            |

说明：

- 本项目已移除 `POLY_USER_ADDRESS` / `POLY_USER` 的运行时依赖。
- `user address` 现在由 `signature_type` 自动推导，不需要单独配置。

## 启动前校验规则

服务启动前会检查 signer/funder/signature_type 组合是否合法：

1. `POLY_SIGNATURE_TYPE` 仅允许 `0/1/2`（或留空）。
2. `signature_type=1/2` 时，必须提供合法 `POLY_FUNDER`。
3. `signature_type=0`（或留空）时：
   - 若同时提供了 signer 和 funder，且二者不一致，启动会失败。
4. `signature_type=1/2` 时：
   - signer 与 funder 不一致是允许的，不会因不一致失败。

## 推荐配置示例

### 直连钱包（EOA）

```env
POLY_SIGNATURE_TYPE=0
# 可选；若设置建议与 signer 一致
POLY_FUNDER=0xyour_signer_address
```

私钥通过本地密钥仓库写入：

```bash
poetry run poly-shield secrets set-private-key
```

### 代理钱包（signature_type=1）

```env
POLY_SIGNATURE_TYPE=1
POLY_FUNDER=0xyour_proxy_wallet
```

### 代理钱包（signature_type=2）

```env
POLY_SIGNATURE_TYPE=2
POLY_FUNDER=0xyour_safe_or_proxy_wallet
```

## 快速自检

运行：

```bash
poetry run poly-shield secrets inspect-private-key
```

重点看这些字段：

- `signer_address`
- `signature_type`
- `configured_funder`
- `effective_user_address`
- `signer_matches_funder`

再启动服务观察启动 banner 中的 `Signer Config Check`：

```bash
poetry run poly-shield serve
```

## 常见错误

### 错误：`POLY_FUNDER is required when POLY_SIGNATURE_TYPE is 1 or 2`

原因：代理模式下缺少 funder。

处理：补充 `POLY_FUNDER`，并确认是合法 EVM 地址。

### 错误：`signer/funder mismatch in direct-wallet mode`

原因：当前是直连模式（`signature_type=0` 或空），但 signer 与 funder 不一致。

处理：

1. 如果你本来就是代理钱包，改成 `POLY_SIGNATURE_TYPE=1` 或 `2`。
2. 如果你是直连钱包，让 `POLY_FUNDER` 与 signer 保持一致（或不设置 funder）。
