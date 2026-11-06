# Plataforma de Inteligência de Conteúdo

Ferramenta para acompanhar perfis, publicações, comentários e tendências, com histórico em banco SQLite, relatórios em CSV e PDF, e laboratório de ideias.

## Como rodar localmente

```
pip install -r requirements.txt
streamlit run app.py
```

## Coleta de dados

A coleta é feita por importação de arquivos CSV, na página "Importar publicações". Não há coleta automática nem scraping.

## Inteligência artificial (opcional)

Para gerar gancho e desenvolvimento das ideias com IA, defina a variável `ANTHROPIC_API_KEY`. Sem ela, a aplicação funciona normalmente, e os campos criativos ficam indicados como indisponíveis. `ANTHROPIC_MODEL` é opcional.

## Privacidade

Os dados importados podem incluir comentários de terceiros. Não versione o banco de dados nem chaves de API. O `.gitignore` já exclui esses arquivos.
