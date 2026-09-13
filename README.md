# RetailSalesDB MCP — VS Code Setup

Connect a Docker-hosted SQL Server database to VS Code Copilot through Model Context Protocol (MCP) using Microsoft's Data API Builder (DAB).

## Architecture

```text
VS Code + GitHub Copilot
          |
          v
        MCP
          |
          v
Data API Builder (DAB)
          |
          v
   localhost:1433
          |
          v
 Docker SQL Server 2019
          |
          v
     RetailSalesDB
       |    |    |
 customers products orders
                  |
             order_items
```

## Database

SQL Server runs in Docker on the local Mac.

Expected connection:
- Server: `localhost,1433`
- Database: `RetailSalesDB`
- User: `sa`
- Authentication: SQL Login

MCP entities:
- `dbo.customers`
- `dbo.products`
- `dbo.orders`
- `dbo.order_items`

The current DAB configuration is read-only.

## Prerequisites

1. Docker Desktop
2. Visual Studio Code
3. .NET SDK 8 or later
4. GitHub Copilot extension for VS Code
5. MSSQL extension for VS Code
6. Microsoft Data API Builder

Check:

```bash
docker --version
dotnet --version
dab --version
```

## Docker SQL Server

Verify the container:

```bash
docker ps
```

Expected container name:

```text
sqlserver
```

Expected port mapping:

```text
0.0.0.0:1433->1433/tcp
```

If stopped:

```bash
docker start sqlserver
```

## Configuration and GitHub security

**Never commit your SQL password.**

The real `dab-config.json` should stay local because it contains the SQL connection string.

The repository should contain:
- `dab-config.template.json` — safe template with a placeholder password
- `.gitignore` — ignores the real `dab-config.json`

Create your local config:

```bash
cp dab-config.template.json dab-config.json
```

Then edit `dab-config.json` and replace `YOUR_SQL_PASSWORD`.

Example:

```text
Server=localhost,1433;Database=RetailSalesDB;User Id=sa;Password=YOUR_PASSWORD;TrustServerCertificate=True;
```

## Install Data API Builder

```bash
dotnet tool install --global Microsoft.DataApiBuilder
```

Verify:

```bash
dab --version
```

If `dab` is not found:

```bash
echo 'export PATH="$PATH:$HOME/.dotnet/tools"' >> ~/.zprofile
source ~/.zprofile
```

## Start DAB / MCP

From the project directory:

```bash
cd ~/Documents/RetailSalesMCP
```

Start:

```bash
dab start --config dab-config.json
```

Successful startup should include:

```text
Now listening on: http://localhost:5000
```

Keep this terminal running while using MCP.

## VS Code MCP configuration

`.vscode/mcp.json` points VS Code to:

```text
http://localhost:5000
```

Example:

```json
{
  "servers": {
    "retail-sales-sql": {
      "type": "http",
      "url": "http://localhost:5000"
    }
  }
}
```

## Test in VS Code

Open GitHub Copilot Chat in Agent mode.

Try:

```text
What MCP tools are available for the RetailSalesDB database?
```

```text
Show me 10 records from the customers table.
```

```text
Describe the customers, products, orders and order_items entities.
```

```text
Show me the 10 most recent orders.
```

```text
Show total sales by month.
```

```text
Find the top 10 products by revenue.
```

```text
Join customers, orders and order_items and show customer-wise sales.
```

## Read-only security

The DAB entities expose read actions only:

```text
READ / SELECT   Allowed
INSERT          Not exposed
UPDATE          Not exposed
DELETE          Not exposed
```

For shared or production use, use proper authentication, a dedicated SQL account with least-privilege permissions, and secure secret management.

## Repository structure

```text
RetailSalesMCP/
│
├── .gitignore
├── README.md
├── dab-config.template.json
│
└── .vscode/
    └── mcp.json
```

Do not commit:

```text
dab-config.json
```

when it contains credentials.

## Troubleshooting

### `dab: command not found`

```bash
source ~/.zprofile
dab --version
```

### .NET 8 missing

```bash
dotnet --list-runtimes
```

Look for `Microsoft.NETCore.App 8.0.x`.

### SQL Server connection fails

```bash
docker ps
```

Confirm `sqlserver` is running and port 1433 is exposed.

### MCP cannot connect

Confirm DAB is running on:

```text
http://localhost:5000
```

and `.vscode/mcp.json` uses the same address.

## Stop the local MCP server

In the terminal running DAB:

```text
Ctrl + C
```

This stops DAB but does not stop the SQL Server container.

## Start again

```bash
cd ~/Documents/RetailSalesMCP
dab start --config dab-config.json
```

## Notes

This is a local development / proof-of-concept setup. Before using it in a shared or production environment, review authentication, authorization, SQL permissions, secrets management, network exposure, auditing, and data privacy requirements.
