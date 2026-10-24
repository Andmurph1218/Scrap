# PROMPT MESTRE — PIPELINE WAGO + DASHBOARD LOCAL

## 1. PAPEL E OBJETIVO

Atue como engenheiro de software sênior, arquiteto de sistemas, especialista em automação de catálogos de e-commerce e QA.

Você deverá analisar o repositório atual e evoluir o projeto Wago para uma aplicação de processamento de produtos com pipeline confiável, execução controlada, geração de arquivos compatíveis com os templates oficiais e dashboard local visual.

**Regra principal:** não reescreva o projeto do zero. Preserve a arquitetura, a linguagem, as dependências e os comandos que já funcionam, salvo quando uma alteração for tecnicamente necessária e justificada.

Trabalhe em fases pequenas, testáveis e reversíveis. Não avance automaticamente para a próxima fase se os critérios de aceite da fase atual não forem atendidos.

## 2. ESCOPO FUNCIONAL OFICIAL

Respeite o seguinte fluxo:

| Etapa | Função | Saída esperada |
|---|---|---|
| 1 | Descobrir links pelo sitemap | `data/raw/links_wago_coletados.json` |
| 2 | Scraping autenticado de produtos | Arquivos master CSV em `data/out/` |
| 3 | Exportar master e checkpoints | Master e arquivos auxiliares |
| 4a | Preencher template Nuvemshop | `data/out/produtos-Nuvemshop-preenchido.csv` |
| 4b | Baixar imagens localmente | `data/out/imagens/<sku>__<slug>/` |
| 5 | Preencher template oficial ML | `data/out/Anunciar-ML-preenchido.xlsx` |

Templates oficiais:

- `templates/produtos-Nuvemshop.csv`
- `templates/Anunciar-ML.xlsx`

A localização real dos templates deverá ser confirmada durante a auditoria. Não criar templates substitutos quando os oficiais estiverem disponíveis.

### Regras de negócio obrigatórias

**Nuvemshop**
- Preservar exatamente o cabeçalho oficial de 31 colunas, a ordem e os nomes dos campos.
- Descartar linhas de exemplo do template e gerar somente os produtos Wago processados.
- Mapear título, identificador URL, SEO, categorias, preço, custo, peso, dimensões, estoque, SKU, MPN, marca, descrição, tags e configurações comerciais.
- Preço de venda: `(custo + IPI) / 0.6`, conforme o plano.
- Custo: custo original do produto.
- Estoque: `2`.
- Marca: `Wago`.
- Configurações previstas: Exibir `SIM`, Frete `NÃO`, Produto físico `SIM`, Visibilidade `Visível`.
- Converter unidades de medidas somente conforme regras identificadas e documentadas no projeto.
- Validar dados ausentes, números inválidos e preços antes da exportação.

**Imagens**
- Ler as URLs da coluna de fotos do master.
- Criar uma pasta por SKU, com o padrão `<sku>__<slug-do-nome>`.
- Salvar imagens sequencialmente, como `01.jpg`, `02.jpg`, etc., respeitando o formato real do conteúdo baixado.
- Não alterar as URLs de origem nem fazer upload pela API da Nuvemshop.
- Tratar timeout, HTTP inválido, arquivo vazio, conteúdo não reconhecido como imagem e falhas parciais.
- Não baixar novamente arquivos válidos sem necessidade.
- Não sobrescrever arquivos de forma destrutiva.

**Mercado Livre**
- Utilizar `templates/Anunciar-ML.xlsx`.
- Preservar as abas, a estrutura do workbook, as validações, os dropdowns e a formatação original.
- Aba padrão: `Outros`; aceitar `--sheet Conectores` e outras abas existentes explicitamente autorizadas.
- Título com até 60 caracteres.
- Condição: `Novo`.
- Código universal: `O produto não tem código cadastrado`.
- Fotos: URLs de origem separadas por vírgula, conforme o modelo.
- Preencher SKU, estoque, preço e descrição a partir do master.
- Dimensões e peso em centímetros inteiros, conforme o contrato do template.
- Tipo de anúncio: `Clássico`.
- Envio: `Mercado Envios`.
- Garantia: `Garantia do vendedor`, duração `12`, unidade `meses`, respeitando as colunas reais.
- Marca: `Wago`.
- Modelo: SKU.
- Não inventar mapeamentos de colunas. Inspecionar a aba e os cabeçalhos reais antes de implementar.

**IPI**
- Preservar o comportamento existente de obtenção do IPI via OCC.
- Não remover nem substituir essa integração sem evidência técnica e testes.
- Registrar erros de consulta e distinguir falha de consulta de IPI efetivamente igual a zero.
- Não gerar preços finais silenciosamente quando o IPI obrigatório estiver indisponível.

**Fora do escopo atual**
- Upload de imagens pela API da Nuvemshop.
- Publicação automática de anúncios no Mercado Livre.
- Geração de templates ML via API.
- Integrações externas não previstas no fluxo.

## 3. FASE 0 — AUDITORIA DO REPOSITÓRIO

Antes de editar arquivos:

1. Inspecione a estrutura do projeto, documentação, configurações, dependências e testes.
2. Identifique linguagem, ponto de entrada, CLI, módulos, integrações e sistema de logging.
3. Localize todos os comandos existentes: `discover`, `scrape`, `export`, `fill-nuvemshop`, `images` e `fill-ml`.
4. Identifique como são armazenados os dados, checkpoints, credenciais e configurações.
5. Examine os templates oficiais, incluindo todas as abas e colunas relevantes.
6. Verifique se existem dados reais que possam ser sobrescritos.
7. Identifique bugs, duplicação de lógica e funcionalidades já implementadas.
8. Crie um relatório com os achados, riscos e plano de alterações.

Não exiba segredos nos logs ou no relatório.

Antes de modificar o código, registre a situação inicial do repositório e os testes que já passam.

### Gate da Fase 0

Entregue o diagnóstico e a lista exata dos arquivos que pretende modificar. Não inicie uma reescrita ampla nem altere os templates oficiais.

## 4. FASE 1 — ESTABILIZAÇÃO DO PIPELINE

Valide as etapas 1, 2 e 3 antes de desenvolver novas funcionalidades.

- Preserve os comandos existentes e sua compatibilidade.
- Corrija somente falhas demonstradas.
- Garanta que erros de scraping não sejam confundidos com produtos processados com sucesso.
- Valide os campos essenciais do master.
- Garanta que os checkpoints possam ser utilizados para recuperação quando isso for compatível com a arquitetura atual.
- Evite duplicação de produtos e perda de registros.
- Adicione testes de regressão.

Não faça scraping de grande volume como parte dos testes automatizados.

### Gate da Fase 1

Os comandos existentes devem funcionar, os testes de regressão devem passar e os formatos de saída devem estar documentados.

## 5. FASE 2 — IMPLEMENTAÇÃO DOS EXPORTADORES

Implemente ou corrija os módulos das etapas 4a, 4b e 5.

### 5.1 `fill-nuvemshop`

- Ler o master validado.
- Ler o template oficial como fonte do cabeçalho.
- Mapear os campos por nome, não por posições presumidas do master.
- Preservar a ordem e a quantidade das 31 colunas.
- Gerar apenas linhas de produtos.
- Validar o arquivo final antes de considerá-lo concluído.
- Produzir um resumo: registros lidos, exportados, rejeitados e motivos das rejeições.

### 5.2 `images`

- Interpretar a coluna de fotos conforme o formato real do master.
- Aceitar múltiplas URLs.
- Criar diretórios seguros, com SKU e slug normalizado.
- Impedir path traversal e nomes de arquivo perigosos.
- Validar o status HTTP, tamanho e conteúdo dos arquivos.
- Usar timeout e tratamento de erros.
- Implementar tentativas limitadas para falhas transitórias.
- Permitir retomada sem repetir downloads válidos.
- Não considerar um download concluído até que o arquivo esteja validado.
- Registrar quantidade de imagens baixadas, ignoradas e com falha por produto.

A extensão do arquivo deve refletir o formato real da imagem, não apenas a extensão informada na URL.

### 5.3 `fill-ml`

- Abrir o workbook oficial sem destruir recursos do Excel.
- Validar a existência da aba solicitada.
- Utilizar `Outros` como padrão, respeitando o comando especificado.
- Identificar a linha de cabeçalho e a primeira linha de dados pelo layout real.
- Acrescentar os produtos sem apagar dados anteriores que precisem ser preservados.
- Evitar duplicatas segundo uma chave estável, preferencialmente SKU.
- Preservar validações, fórmulas, dropdowns, estilos e outras características existentes.
- Salvar em um arquivo de saída distinto do template original.
- Reabrir o arquivo gerado e validar a integridade do workbook.

Se a biblioteca utilizada não preservar algum recurso do Excel, documente a limitação e proponha uma solução compatível antes de alterar o template.

### Regras transversais

- Usar escrita segura: gerar em arquivo temporário, validar e somente então finalizar a saída.
- Não substituir silenciosamente arquivos válidos.
- Disponibilizar modo de simulação, se compatível com o projeto.
- Manter rastreabilidade entre produto, SKU, arquivo de origem e resultado.
- Gerar relatórios legíveis de sucesso e falha.
- Não ocultar exceções importantes.
- Não converter dados ausentes em valores fictícios.

### Gate da Fase 2

Confirmar:
1. CSV Nuvemshop com cabeçalho oficial e N produtos.
2. Uma pasta por SKU com pelo menos uma imagem válida quando houver URL válida e acessível.
3. XLSX ML que abre corretamente e contém as linhas na aba escolhida.

Também testar entradas vazias, SKU duplicado, fotos inválidas, preços ausentes, IPI indisponível e abas inexistentes.

## 6. FASE 3 — DASHBOARD LOCAL VISUAL

Implemente uma interface web local para executar e acompanhar a aplicação.

### 6.1 Auditoria tecnológica

Antes de escolher o framework, verifique as dependências existentes.

- Se já houver framework web, reutilize-o quando adequado.
- Caso contrário, selecione a alternativa mais simples e sustentável compatível com a linguagem do projeto.
- Não introduza uma stack complexa sem necessidade.
- A aplicação deve funcionar localmente, sem depender de serviços externos para exibir o dashboard.

A interface deverá ser acessada por um endereço local, preferencialmente `http://127.0.0.1:<porta>`, com a porta configurável.

### 6.2 Layout do dashboard

Criar uma interface profissional, responsiva e em português brasileiro.

**Cabeçalho**
- Nome: Wago Pipeline.
- Estado da aplicação.
- Estado da última execução.
- Data e hora da última atualização.
- Botão para atualizar informações.

**Visão geral**
- Produtos descobertos.
- Produtos processados.
- Produtos com falha.
- Produtos exportados.
- Imagens baixadas.
- Execuções concluídas e com falha.

Os indicadores devem ser calculados a partir de dados reais, logs estruturados e relatórios persistidos. Não usar números fictícios.

**Pipeline visual**

Exibir seis cartões ou etapas conectadas:

1. Discover links.
2. Scrape autenticado.
3. Export master/checkpoints.
4. Preencher Nuvemshop.
5. Baixar imagens.
6. Preencher Mercado Livre.

Cada etapa deverá mostrar:
- Status: aguardando, executando, concluída, concluída com avisos ou falha.
- Data da última execução.
- Duração.
- Quantidade de registros processados, quando aplicável.
- Mensagem resumida do resultado.
- Ação para executar a etapa individualmente.

**Controles de execução**
- Executar etapa individual.
- Executar pipeline completo na ordem correta.
- Informar limite de produtos para scraping.
- Escolher a aba de destino do Mercado Livre entre as abas reais do template.
- Selecionar o master de entrada entre arquivos autorizados.
- Visualizar uma prévia das configurações antes de iniciar.
- Confirmar operações que possam gerar ou substituir saídas.
- Solicitar cancelamento cooperativo de tarefas em execução.

A execução completa deverá respeitar as dependências. Não executar exportadores antes de existir um master válido.

**Logs em tempo real**
- Mostrar eventos recentes.
- Exibir horário, etapa, nível e mensagem.
- Diferenciar informação, aviso e erro.
- Permitir filtrar por etapa e nível.
- Permitir consultar o histórico de execuções.
- Evitar exibir senhas, cookies, tokens, cabeçalhos de autenticação ou dados sensíveis.

**Arquivos gerados**
- Listar arquivos existentes em `data/out/`.
- Exibir nome, tamanho, data e tipo.
- Mostrar quantidade de produtos nos relatórios, quando disponível.
- Permitir abrir a pasta de saída por mecanismo local seguro, quando suportado.
- Permitir baixar arquivos gerados pela interface apenas por rotas que validem os caminhos autorizados.

**Detalhes por execução**
- ID da execução.
- Etapas executadas.
- Início, fim e duração.
- Produtos processados.
- Erros e avisos.
- Arquivos de saída.
- Status final.

**Configurações**
- Diretórios de entrada e saída autorizados.
- Caminhos dos templates.
- Limite de produtos.
- Timeouts e tentativas de download.
- Aba padrão do Mercado Livre.
- Parâmetros não secretos da aplicação.

Credenciais devem permanecer em variáveis de ambiente ou no mecanismo seguro já utilizado pelo projeto. Não exibir valores secretos na interface.

### 6.3 Arquitetura de execução

O dashboard deve controlar o pipeline por uma camada de serviços interna e reutilizar a lógica dos comandos existentes.

- Não duplicar a implementação do scraper.
- Não executar comandos arbitrários recebidos do navegador.
- Utilizar uma lista fixa de operações autorizadas.
- Validar parâmetros no servidor.
- Executar tarefas longas fora do ciclo de requisição HTTP.
- Exibir progresso real, não percentuais simulados.
- Persistir histórico e eventos para que possam ser consultados depois de reiniciar a interface.
- Garantir que a interface continue responsiva durante o scraping e os downloads.
- Impedir duas execuções concorrentes conflitantes.
- Implementar cancelamento cooperativo, sem interromper gravações no meio de um arquivo.
- Registrar códigos de saída e exceções.
- Recuperar-se de falhas sem marcar a execução inteira como concluída.

Utilize o mecanismo de persistência mais simples compatível com o projeto. Se não houver infraestrutura existente, uma base local leve pode ser utilizada para histórico e controle de execuções.

### 6.4 Segurança do dashboard

- Por padrão, vincular o servidor a `127.0.0.1`, não a todas as interfaces de rede.
- Não expor o dashboard à internet.
- Validar entradas e caminhos de arquivos.
- Impedir acesso a arquivos fora dos diretórios autorizados.
- Não disponibilizar um endpoint genérico de execução de shell.
- Proteger operações destrutivas com confirmação.
- Não permitir que requisições concorrentes corrompam arquivos.
- Aplicar limites razoáveis a uploads, downloads e consultas, caso esses recursos existam.
- Não habilitar upload de arquivos arbitrários sem necessidade.
- Não registrar credenciais nos logs.
- Documentar as condições de acesso local e eventuais riscos da rede local.

### 6.5 Inicialização

Criar um comando ou script de inicialização compatível com o sistema operacional identificado no repositório.

Preferência:
- Inicializar o backend.
- Verificar a disponibilidade da aplicação.
- Exibir o endereço local no terminal.
- Abrir o navegador automaticamente apenas se isso for solicitado ou configurado.
- Encerrar os processos filhos de maneira controlada.
- Exibir mensagens claras em caso de porta ocupada ou configuração inválida.

Não presumir Windows, Linux, Node.js ou Python antes da auditoria.

### Gate da Fase 3

O dashboard deve permitir iniciar uma etapa real, acompanhar sua execução, consultar logs e identificar os arquivos produzidos. Deve continuar responsivo e impedir execuções conflitantes.

## 7. FASE 4 — TESTES INTEGRADOS

Criar testes automatizados para:

- Mapeamento de colunas da Nuvemshop.
- Fórmula de preço e tratamento de IPI.
- Geração do CSV com exatamente 31 colunas.
- Normalização de SKU e slug.
- Download, validação e retomada de imagens.
- Preservação das características do XLSX.
- Seleção de abas do Mercado Livre.
- Tratamento de erros e retomada de tarefas.
- Validação de parâmetros da interface.
- Prevenção de execuções concorrentes.
- Controle de acesso aos arquivos locais.
- Persistência do histórico de execuções.
- Inicialização e encerramento do dashboard.

Usar fixtures e dados sintéticos nos testes automatizados. Não depender de login real, APIs externas ou scraping ao vivo para validar a lógica central.

Criar testes específicos para os arquivos de saída e reabri-los depois da geração.

## 8. FASE 5 — HOMOLOGAÇÃO CONTROLADA

Executar o fluxo inicialmente com um limite pequeno de produtos.

Ordem sugerida:
1. Descoberta e validação dos links.
2. Scraping limitado.
3. Exportação do master.
4. Validação do CSV Nuvemshop.
5. Download de imagens.
6. Validação do XLSX Mercado Livre.
7. Conferência visual no dashboard.

Comparar os resultados com os templates oficiais.

Não realizar publicação de produtos, upload de imagens para plataformas ou alterações em contas externas.

Aumentar o volume somente após os resultados serem aprovados.

## 9. DOCUMENTAÇÃO E ENTREGA

Atualizar ou criar a documentação do projeto com:

- Arquitetura e fluxo do pipeline.
- Pré-requisitos e instalação.
- Configuração de variáveis de ambiente.
- Localização dos templates.
- Comandos CLI.
- Como iniciar e encerrar o dashboard.
- Como executar cada etapa.
- Como executar o pipeline completo.
- Localização dos arquivos de saída.
- Como consultar logs e histórico.
- Como recuperar uma execução interrompida.
- Como realizar backup dos dados.
- Limitações conhecidas.
- Procedimento para diagnosticar erros comuns.

Criar um arquivo de exemplo de configuração sem segredos, caso necessário.

## 10. PROTOCOLO DE TRABALHO NO CURSOR

Trabalhe por entregas pequenas.

Para cada fase:

1. Explique o objetivo.
2. Liste os arquivos que serão criados ou alterados.
3. Apresente os riscos e as decisões técnicas.
4. Implemente somente o escopo daquela fase.
5. Execute os testes relevantes.
6. Inspecione o diff e procure regressões.
7. Apresente os resultados reais dos testes.
8. Atualize a documentação.
9. Informe o critério de aceite e o que ainda falta.

Não invente resultados de testes nem afirme que uma integração foi validada sem executá-la.

Não avance se houver falhas críticas. Se um teste não puder ser executado por falta de credenciais, rede ou dependências, documente essa limitação e separe a validação pendente dos testes efetivamente aprovados.

Preserve alterações preexistentes do usuário. Não reverta mudanças que não foram criadas durante a execução deste trabalho.

Evite refatorações amplas, migrações desnecessárias e novas dependências sem justificativa.

## 11. DEFINIÇÃO DE PRONTO

O projeto só poderá ser considerado concluído quando:

- Os comandos originais continuarem compatíveis.
- O pipeline completo funcionar na ordem definida.
- O CSV respeitar o template Nuvemshop de 31 colunas.
- As imagens forem baixadas e organizadas por SKU.
- O XLSX preservar a estrutura e os recursos relevantes do template oficial.
- Os critérios de aceite do plano forem comprovados.
- O dashboard local iniciar, executar etapas reais e mostrar o estado das tarefas.
- Os logs e o histórico forem consultáveis.
- Os testes relevantes passarem.
- As limitações e os procedimentos de operação estiverem documentados.

**COMECE AGORA PELA FASE 0.** Inspecione o repositório e apresente o diagnóstico antes de realizar alterações. Depois, implemente uma fase por vez, solicitando validação antes de prosseguir quando houver decisões arquiteturais, risco de perda de dados ou alterações potencialmente destrutivas.