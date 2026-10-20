# Comunicação, perfis e diagnóstico

Estas melhorias pertencem ao **configurador**. O firmware personalizado que grava
`axis.N.encvel` e `axis.N.maxtorquerate` continua sendo necessário para garantir a
persistência desses parâmetros após desligar a placa. O configurador detecta os
comandos disponíveis, mas não acrescenta armazenamento a um firmware antigo.

## Usar os novos controles

1. Conecte a placa e abra a aba do eixo.
2. Clique em **Advanced axis settings**. Os valores são lidos da placa.
3. Ajuste **Torque rate limit**, em counts/ms. Zero desativa esse limite;
   **Disable limit** prepara zero, e **Apply and verify** efetiva a alteração.
4. **Prefer fresh encoder velocity** aparece somente se o firmware anunciar
   leitura e escrita de `encvel`.
5. Clique em **Apply and verify**. A confirmação exige resposta `OK` ao SET e
   uma nova leitura GET com o valor esperado. Uma falha não é exibida como sucesso.

O campo aceita todo o intervalo uint32 de `maxtorquerate`, até 4294967295.
Não há alteração automática de parâmetros apenas ao abrir essa janela.

Para verificar a persistência física, altere o valor, espere a confirmação,
reinicie a placa, reconecte e use **Read from board**. Esse teste precisa da placa;
os testes automatizados não comprovam a gravação na flash do hardware.

## Perfis

A aplicação consulta as classes ativas e lê os parâmetros antes de escrever.
Depois aplica cada valor, espera o reconhecimento e faz a leitura de confirmação.
O perfil só é marcado como ativo quando todos os parâmetros foram confirmados.
Se um parâmetro opcional não existir no firmware, o configurador informa aplicação
parcial. Falhas interrompem a sequência e identificam quantas alterações já foram
confirmadas. Não há reversão automática: alterações já enviadas podem permanecer.

`encvel` e `maxtorquerate` entram na captura do perfil quando disponíveis. Perfis
antigos que não contêm esses campos continuam funcionando. O botão de salvar na
flash continua explícito; aplicar um perfil não salva automaticamente todos os
outros parâmetros da placa.

O arquivo `profiles.json` usa arquivo temporário, sincronização e substituição
atômica. `profiles.json.bak` guarda a versão anterior válida. Um arquivo danificado
é preservado como `profiles.json.corrupt` quando a cópia de segurança é recuperada.
Se ambos estiverem inválidos, a aplicação abre com configurações padrão em memória
e bloqueia a gravação para preservar os arquivos. Feche o configurador, recupere
uma cópia válida e reabra. O local do arquivo permanece igual ao das versões
anteriores, inclusive `$XDG_CONFIG_HOME/openffboard` no Linux.

## Comunicação e reconexão

- Pacotes têm no máximo 1024 **bytes UTF-8**, preservam `;` e são enviados com
  espaçamento de até 30 ms entre pacotes. Escritas parciais preservam o restante.
- Endereço zero é válido. `ERR`, `NOT_FOUND`, resposta inválida, fila cheia,
  falha de transporte e timeout têm caminhos de erro distintos.
- Requisições iguais são serializadas. O prazo padrão é de 3 segundos, medido por
  relógio monotônico; o heartbeat usa 2 segundos e tolera uma falha isolada.
- O protocolo não tem identificador de transação. Após timeout, a mesma chave
  fica bloqueada até consumir a resposta atrasada ou reconectar. Nenhuma escrita
  é repetida automaticamente. Isso também evita que uma resposta atrasada seja
  usada para confirmar uma alteração posterior.
- A reconexão é opcional em **Reconnect this device after connection loss**.
  Faz até oito tentativas com intervalos crescentes, no máximo 15 segundos.
  Com número de série USB, acompanha o mesmo dispositivo mesmo que a porta mude.
  Sem número de série, exige o mesmo VID/PID e caminho da porta; nesse caso não é
  possível distinguir duas placas idênticas que ocupem a mesma porta.
- **Disconnect**, **Cancel reconnect** e a saída da aplicação cancelam tentativas.
  Comandos pendentes e perfis não são reaplicados ao reconectar.

Não use o terminal para enviar comandos concorrentes aos mesmos parâmetros
durante uma aplicação de perfil ou uma alteração verificada. Respostas manuais
e de outros clientes não possuem identificadores que permitam diferenciá-las.

## Diagnóstico e CSV

Na aba do eixo, abra **Live diagnostics / CSV** e clique em **Start new capture**.
A coleta é passiva: não cria efeitos, não ativa torque e não muda ganhos, limites
ou configuração CAN do ODrive. Coleta até 5 amostras/s e mantém as últimas 600.
Uma nova captura limpa o histórico anterior; exporte antes se quiser preservá-lo.

O gráfico permite escolher torque comandado, `hidrate`, `cfrate`, intervalo de
amostragem do PC, último `dt` do firmware, frequência entre snapshots e Iq fresco.
Sem `axis.diag`, usa `axis.curtorque`; campos indisponíveis ficam ausentes.
Timeout ou desconexão interrompem a coleta e preservam as amostras para exportação.
Fechar a janela também interrompe a coleta.

Para incluir ODrive, marque **Include ODrive instance** e escolha a instância
`odrv.N` correta. O número é a instância do comando, não o CAN node ID. Se usar
firmware **0.5.6**, selecione essa versão para decodificar erros de eixo; em outras
versões mantenha **raw / other firmware**. O valor hexadecimal e bits desconhecidos
são preservados. `MOTOR_FAILED` indica que é preciso consultar o erro detalhado do
motor pelo odrivetool; não é interpretado como um bitmap de erros do motor.

Interpretação dos dados:

- Torque em counts e `queued_torque_nm` são comandos, não torque medido no eixo.
- `hidrate` e `cfrate` são estimativas de atualizações de efeitos recebidas do jogo,
  não a frequência da malha FFB.
- Tempos `host_*` incluem polling, processamento e escalonamento do PC. Não são
  medições de latência USB nem de jitter da malha do motor.
- `axis_dt_us` é o último intervalo publicado; mínimos e máximos são acumulados
  pelo firmware. Snapshots a 5 Hz não fornecem a distribuição completa de jitter.
- Iq é corrente em ampères. Para o gráfico de Iq fresco, a sequência deve ser
  diferente de zero e a idade informada pelo firmware não pode exceder 50 ms.
- Cada resposta recebe um horário do PC. As leituras são assíncronas; não trate
  uma linha CSV como medição simultânea de todos os sinais.

Definições dos erros: [ODrive fw-v0.5.6, enums.py](https://github.com/odriverobotics/ODrive/blob/fw-v0.5.6/tools/odrive/enums.py).

## Executar e validar

```sh
python -m pip install -r requirements.txt
python main.py
```

```sh
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
```

No PowerShell, defina `$env:QT_QPA_PLATFORM="offscreen"` antes de executar o segundo
comando. Os testes PTY são exclusivos de POSIX; os demais não precisam de placa.

O workflow **Build Configurator** executa os testes e gera os pacotes Windows e
macOS. No GitHub Actions, baixe o artifact Windows da execução correspondente à
branch desejada, extraia o ZIP inteiro e execute `OpenFFBoard.exe` dentro da pasta.
Não mova apenas o EXE: as bibliotecas que o acompanham são necessárias.

O conjunto local cobre empacotamento, endereçamento, filas, respostas atrasadas,
escrita e leitura real por PTY, falhas de perfis, recuperação de arquivos, detecção
de recursos antigos, diálogos Qt, CSV e identificação para reconexão. A estabilidade
prolongada, a reconexão USB física e o funcionamento no seu Windows 10 precisam ser
confirmados com a placa conectada.

## Estudo de torque/corrente e slots de slew

Na janela de diagnóstico, **Torque / current study** abre a captura dos quadros
Iq registrados pelo firmware. Informe o Kt do motor (valor inicial 1,11 Nm/A),
instância do ODrive e rótulo. **Stop → Export trace CSV** preserva os dados;
**Analyze / export JSON** agrupa erro de corrente por RPM, sentido e quadrante.
O histórico anterior ao início é descartado. Perdas, reinicializações e descarte
dos registros mais antigos pelo limite de 12000 amostras são informados.

**Advanced axis settings → Persistent slew profiles** permite gravar três taxas
por eixo e ativar um slot. Zero desativa o limite; editar um slot ativo aplica
sua nova taxa. As operações exigem reconhecimento e leitura de confirmação.
A gravação permanente é responsabilidade do novo firmware. Slots de slew na
placa são independentes dos perfis gerais JSON do configurador.

Firmwares antigos continuam funcionando: comandos novos só são enviados se
anunciados na lista de recursos. Não são criadas forças, não são retunados ganhos
e não são configuradas taxas CAN durante a captura. Sem quadros Get_Iq, não há
corrente a exibir. Kt × Iq é uma estimativa eletromagnética, não torque medido
no eixo. Timestamps de RX e idades não comprovam latência física.

Protocolo completo, requisitos de CAN, migração e roteiro de ensaio:
[torque-study-pt-BR.md](https://github.com/Mauricio-Maciel-Soares/OpenFFBoard/blob/torque-observability-profiles-v1.17.1/docs/torque-study-pt-BR.md).
